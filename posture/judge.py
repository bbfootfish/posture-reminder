"""Turning smoothed metrics into an out-of-line issue class."""

from __future__ import annotations

from .models import HEAD_DOWN, LEAN, TOO_CLOSE, Baseline, Metrics


def _rescale_s_norm(baseline: Baseline, full_frame_width: int, crop_width: int) -> float:
    """Convert a baseline ``s_norm`` recorded on the full frame to crop scale.

    ``s_norm`` is shoulder width / frame width.  Once pose detection runs on a
    cropped region, the divisor shrinks with the crop, so every live reading
    grows by ``full_width / crop_width``.  The stored baseline keeps the scale it
    was recorded at, so it must be converted before comparison or the threshold
    is meaningless.

    This was a live defect: with a 1920 -> 767 px crop the baseline 0.06369
    became an effective threshold of 0.0732, which 100% of real samples cleared,
    making ``lean`` unreachable and pinning every forward collapse to
    ``too_close`` (measured over 413 child-present samples).
    """
    if crop_width <= 0:
        raise ValueError("crop_width must be > 0")
    if full_frame_width <= 0:
        raise ValueError("full_frame_width must be > 0")
    return baseline.s_norm * (full_frame_width / crop_width)


def thresholds_for(
    baseline: Baseline,
    *,
    head_up_ratio: float = 0.50,
    head_fwd_drop: float = 15.0,
    too_close_ratio: float = 1.15,
    full_frame_width: int = 0,
    crop_width: int = 0,
) -> dict[str, float]:
    """Return the thresholds the judge compares a sample against.

    Kept as a small pure function so the dashboard can explain a decision with
    the same numbers the judge used, without duplicating policy.

    Pass ``full_frame_width``/``crop_width`` when inference runs on a crop; the
    stored ``s_norm`` baseline is then rescaled to the crop's coordinate system.
    Omitting both keeps the historical (uncropped) behaviour.
    """
    s_norm_base = baseline.s_norm
    # Rescale whenever the caller supplied either dimension; passing one of them
    # as 0 is a configuration error, not a request to skip the correction.
    if full_frame_width or crop_width:
        s_norm_base = _rescale_s_norm(baseline, full_frame_width, crop_width)
    return {
        "head_up": baseline.head_up * head_up_ratio,
        "head_fwd": baseline.head_fwd - head_fwd_drop,
        "too_close_s_norm": s_norm_base * too_close_ratio,
    }


class PostureJudge:
    """The two outlier rules from the ticket.

    A sample is out of line when the head is much lower than the calibrated
    writing baseline, or when the ear-shoulder-hip angle has collapsed by
    more than 15 degrees.  The distance proxy is *not* a trigger on its own:
    the ticket only defines those two triggers.  It is used to name a
    forward collapse near the desk ("too close") instead of a sideways lean.
    """

    def __init__(
        self,
        baseline: Baseline,
        *,
        head_up_ratio: float = 0.50,
        head_fwd_drop: float = 15.0,
        too_close_ratio: float = 1.15,
        full_frame_width: int = 0,
        crop_width: int = 0,
    ) -> None:
        if not baseline.is_usable():
            raise ValueError("baseline is not usable")
        if bool(full_frame_width) != bool(crop_width):
            raise ValueError("pass both full_frame_width and crop_width, or neither")
        self.baseline = baseline
        thresholds = thresholds_for(
            baseline,
            head_up_ratio=head_up_ratio,
            head_fwd_drop=head_fwd_drop,
            too_close_ratio=too_close_ratio,
            full_frame_width=full_frame_width,
            crop_width=crop_width,
        )
        self.head_up_threshold = thresholds["head_up"]
        self.head_fwd_threshold = thresholds["head_fwd"]
        self.too_close_threshold = thresholds["too_close_s_norm"]

    def classify(self, metrics: Metrics) -> str | None:
        head_down = metrics.head_up < self.head_up_threshold
        forward = metrics.head_fwd < self.head_fwd_threshold
        if not head_down and not forward:
            return None
        if head_down:
            return HEAD_DOWN
        if metrics.s_norm >= self.too_close_threshold:
            return TOO_CLOSE
        return LEAN
