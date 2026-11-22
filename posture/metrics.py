"""Metric extraction from MediaPipe Pose landmarks.

Only the three side-view metrics from the ticket are computed.  ``ear_tilt``,
``sh_tilt`` and ``lat_off`` are intentionally absent: both ears overlap in a
side view, so those metrics degenerate and must not be used.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .models import Metrics

# MediaPipe Pose landmark indices.
NOSE = 0
LEFT_EAR = 7
RIGHT_EAR = 8
LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
LEFT_HIP = 23
RIGHT_HIP = 24

MIN_VISIBILITY = 0.55
MIN_SHOULDER_PX = 20.0
# False poses detected on empty furniture are tiny in the frame (~2% width)
# while the real seated subject measured 8.7% in the ticket.  A frame-relative
# floor rejects them; 6% keeps healthy margin under the real value.
MIN_SHOULDER_PCT = 6.0
# This gate originally assumed a *side* view, where a seated subject's nose sits
# above the shoulder line, and used +0.03 to reject empty-chair false poses whose
# nose fell below the shoulders.  That assumption does not hold for the installed
# overhead-rear camera: a child drawing while leaning over the desk puts the nose
# below the shoulder line in 74% of frames (measured median -0.142 over 119
# verified child-present frames, p5 -0.389).  The old floor rejected 75% of real
# posture.  False poses are now handled upstream by the ROI crop (72.5% -> 0%
# over 40 empty-chair frames), so this gate only needs to catch broken skeletons.
# -0.45 admits every plausible lean (-0.40 already covers 96%) while still
# rejecting the anatomically impossible.
MIN_HEAD_UP = -0.45
# Kept for the ROOT check that a real sit-down produces a head above the pelvis.
MIN_HEAD_UP_STRICT = 0.03


@dataclass(frozen=True)
class PoseSample:
    """A usable pose plus the raw numbers needed by the camera audit."""

    metrics: Metrics
    shoulder_px: float
    frame_width: int
    ear_distance_px: float
    ear_shoulder_ratio: float
    hip_visible: bool


@dataclass(frozen=True)
class PoseDiagnostics:
    """Audit-only facts that must be measured even when metrics are invalid.

    The camera audit has to distinguish "no pose at all" from "pose found but
    hips are hidden".  ``extract_sample`` deliberately returns ``None`` in the
    latter case, so the audit uses this lighter view of the same landmarks.
    """

    detected: bool
    hip_visible: bool | None = None
    ear_shoulder_ratio: float | None = None
    shoulder_width_pct: float | None = None
    head_fwd: float | None = None

    @classmethod
    def not_detected(cls) -> PoseDiagnostics:
        return cls(detected=False)


def _visibility(landmark: Any) -> float:
    value = getattr(landmark, "visibility", 1.0)
    return 1.0 if value is None else float(value)


def _pixel(landmark: Any, width: int, height: int) -> tuple[float, float]:
    return float(landmark.x) * width, float(landmark.y) * height


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle_degrees(
    a: tuple[float, float], vertex: tuple[float, float], b: tuple[float, float]
) -> float:
    """Angle at ``vertex`` between the rays to ``a`` and ``b``, in degrees."""
    v1 = (a[0] - vertex[0], a[1] - vertex[1])
    v2 = (b[0] - vertex[0], b[1] - vertex[1])
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 0.0
    cosine = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    cosine = max(-1.0, min(1.0, cosine))
    return math.degrees(math.acos(cosine))


def _best_side(
    landmarks: Sequence[Any], min_visibility: float = MIN_VISIBILITY
) -> tuple[int, int, int] | None:
    """Return the visible (ear, shoulder, hip) triple with the best visibility.

    A side view hides the far side; choosing the more visible side keeps the
    ear-shoulder-hip angle usable without ever averaging across the body.
    """
    candidates = (
        (LEFT_EAR, LEFT_SHOULDER, LEFT_HIP),
        (RIGHT_EAR, RIGHT_SHOULDER, RIGHT_HIP),
    )
    best: tuple[float, tuple[int, int, int]] | None = None
    for ear, shoulder, hip in candidates:
        score = min(
            _visibility(landmarks[ear]),
            _visibility(landmarks[shoulder]),
            _visibility(landmarks[hip]),
        )
        if score < min_visibility:
            continue
        if best is None or score > best[0]:
            best = (score, (ear, shoulder, hip))
    return None if best is None else best[1]


def extract_sample(
    landmarks: Sequence[Any] | None,
    frame_width: int,
    frame_height: int,
    *,
    min_visibility: float = MIN_VISIBILITY,
    min_shoulder_px: float = MIN_SHOULDER_PX,
    min_shoulder_pct: float = MIN_SHOULDER_PCT,
    min_head_up: float = MIN_HEAD_UP,
) -> PoseSample | None:
    """Turn one MediaPipe landmark list into a :class:`PoseSample`.

    Returns ``None`` when a required keypoint is not visible enough, when the
    shoulders are too narrow (absolute or frame-relative), when the head-up
    ratio is physically implausible, or when the input frame is unusable.
    The extra gates reject the high-confidence false poses MediaPipe emits on
    empty chairs and clutter, which pass the visibility check with 1.0.
    """
    if not landmarks or len(landmarks) <= RIGHT_HIP or frame_width <= 0:
        return None

    # Both shoulders and the nose are required by the metric definitions.
    required = (NOSE, LEFT_SHOULDER, RIGHT_SHOULDER)
    if any(_visibility(landmarks[i]) < min_visibility for i in required):
        return None

    nose = _pixel(landmarks[NOSE], frame_width, frame_height)
    left_shoulder = _pixel(landmarks[LEFT_SHOULDER], frame_width, frame_height)
    right_shoulder = _pixel(landmarks[RIGHT_SHOULDER], frame_width, frame_height)

    shoulder_px = _distance(left_shoulder, right_shoulder)
    shoulder_pct = shoulder_px / float(frame_width) * 100.0
    if shoulder_px < min_shoulder_px or shoulder_pct < min_shoulder_pct:
        return None

    side = _best_side(landmarks, min_visibility)
    if side is None:
        return None
    ear_index, shoulder_index, hip_index = side
    if _visibility(landmarks[shoulder_index]) < min_visibility:
        return None

    ear = _pixel(landmarks[ear_index], frame_width, frame_height)
    shoulder = _pixel(landmarks[shoulder_index], frame_width, frame_height)
    hip = _pixel(landmarks[hip_index], frame_width, frame_height)

    shoulder_mid_y = (left_shoulder[1] + right_shoulder[1]) / 2.0
    head_up = (shoulder_mid_y - nose[1]) / shoulder_px
    if head_up < min_head_up:
        return None
    head_fwd = _angle_degrees(ear, shoulder, hip)
    s_norm = shoulder_px / float(frame_width)

    left_ear = _pixel(landmarks[LEFT_EAR], frame_width, frame_height)
    right_ear = _pixel(landmarks[RIGHT_EAR], frame_width, frame_height)
    ear_distance_px = _distance(left_ear, right_ear)

    return PoseSample(
        metrics=Metrics(s_norm=s_norm, head_up=head_up, head_fwd=head_fwd),
        shoulder_px=shoulder_px,
        frame_width=frame_width,
        ear_distance_px=ear_distance_px,
        ear_shoulder_ratio=ear_distance_px / shoulder_px,
        hip_visible=_visibility(landmarks[hip_index]) >= min_visibility,
    )


def diagnostics_from_landmarks(
    landmarks: Sequence[Any] | None, frame_width: int, frame_height: int
) -> PoseDiagnostics:
    """Measure detection, hips, view ratio and signal without gating frames.

    ``detected`` is true as soon as MediaPipe returns a pose, even if the
    visibility checks reject the frame for the metric path.
    """
    if not landmarks or len(landmarks) <= RIGHT_HIP or frame_width <= 0:
        return PoseDiagnostics.not_detected()

    left_shoulder = _pixel(landmarks[LEFT_SHOULDER], frame_width, frame_height)
    right_shoulder = _pixel(landmarks[RIGHT_SHOULDER], frame_width, frame_height)
    shoulder_px = _distance(left_shoulder, right_shoulder)
    shoulders_visible = (
        _visibility(landmarks[LEFT_SHOULDER]) >= MIN_VISIBILITY
        and _visibility(landmarks[RIGHT_SHOULDER]) >= MIN_VISIBILITY
    )

    shoulder_width_pct: float | None = None
    ratio: float | None = None
    if shoulders_visible and shoulder_px > 0:
        shoulder_width_pct = shoulder_px / float(frame_width) * 100.0
        left_ear = _pixel(landmarks[LEFT_EAR], frame_width, frame_height)
        right_ear = _pixel(landmarks[RIGHT_EAR], frame_width, frame_height)
        ratio = _distance(left_ear, right_ear) / shoulder_px

    hip_visible = (
        max(
            _visibility(landmarks[LEFT_HIP]),
            _visibility(landmarks[RIGHT_HIP]),
        )
        >= MIN_VISIBILITY
    )

    head_fwd: float | None = None
    side = _best_side(landmarks)
    if side is not None:
        ear_index, shoulder_index, hip_index = side
        ear = _pixel(landmarks[ear_index], frame_width, frame_height)
        shoulder = _pixel(landmarks[shoulder_index], frame_width, frame_height)
        hip = _pixel(landmarks[hip_index], frame_width, frame_height)
        head_fwd = _angle_degrees(ear, shoulder, hip)

    return PoseDiagnostics(
        detected=True,
        hip_visible=hip_visible,
        ear_shoulder_ratio=ratio,
        shoulder_width_pct=shoulder_width_pct,
        head_fwd=head_fwd,
    )


def metrics_from_landmarks(
    landmarks: Sequence[Any] | None, frame_width: int, frame_height: int
) -> Metrics | None:
    sample = extract_sample(landmarks, frame_width, frame_height)
    return None if sample is None else sample.metrics
