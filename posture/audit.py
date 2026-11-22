"""Camera placement audit, shared by ``POST /audit`` and ``tools/audit.py``.

The audit decides whether the current camera view is good enough for the
side-view metrics to mean anything.  If it fails, the service must not speak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Any

from .config import Settings
from .metrics import PoseDiagnostics
from .smoothing import RollingMedian

FRONTAL_RATIO_RANGE = (0.50, 0.80)
# ear-shoulder ratio: inter-ear distance / shoulder width.  A pure side view
# gives a small ratio, a frontal view a large one.  The installed camera looks
# from the rear quarter, so live measurements drift between 0.30 and 0.41 as the
# child turns her head -- straddling the original 0.35 ceiling and failing the
# audit intermittently.  The upper bound is widened to 0.45 so the geometric
# property of this installation is accepted without letting a frontal view in.
SIDE_RATIO_RANGE = (0.15, 0.45)


@dataclass(frozen=True)
class AuditObservation:
    """One frame's contribution to the audit (grey frames are excluded).

    ``detected`` is true when MediaPipe returned a pose.  The remaining
    fields are optional so a pose with hidden hips still counts towards the
    detection rate and the hip-visibility check.
    """

    at: datetime
    detected: bool
    hip_visible: bool | None = None
    ear_shoulder_ratio: float | None = None
    shoulder_width_pct: float | None = None
    head_fwd: float | None = None

    @classmethod
    def from_diagnostics(cls, at: datetime, diagnostics: PoseDiagnostics) -> AuditObservation:
        return cls(
            at=at,
            detected=diagnostics.detected,
            hip_visible=diagnostics.hip_visible,
            ear_shoulder_ratio=diagnostics.ear_shoulder_ratio,
            shoulder_width_pct=diagnostics.shoulder_width_pct,
            head_fwd=diagnostics.head_fwd,
        )


@dataclass
class AuditReport:
    passed: bool
    view: str
    duration_seconds: float
    frames: int
    pose_frames: int
    detection_rate: float
    ear_shoulder_ratio: float | None
    shoulder_width_pct: float | None
    hip_visible_rate: float | None
    head_fwd_jitter_deg: float
    head_fwd_range_5s_deg: float
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        def rounded(value: float | None, digits: int = 3) -> float | None:
            return None if value is None else round(value, digits)

        return {
            "passed": self.passed,
            "view": self.view,
            "duration_seconds": round(self.duration_seconds, 1),
            "frames": self.frames,
            "pose_frames": self.pose_frames,
            "detection_rate": rounded(self.detection_rate),
            "ear_shoulder_ratio": rounded(self.ear_shoulder_ratio),
            "shoulder_width_pct": rounded(self.shoulder_width_pct, 2),
            "hip_visible_rate": rounded(self.hip_visible_rate),
            "head_fwd_jitter_deg": rounded(self.head_fwd_jitter_deg, 2),
            "head_fwd_range_5s_deg": rounded(self.head_fwd_range_5s_deg, 2),
            "checks": self.checks,
            "reasons": self.reasons,
        }


def classify_view(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if SIDE_RATIO_RANGE[0] <= ratio <= SIDE_RATIO_RANGE[1]:
        return "side"
    if FRONTAL_RATIO_RANGE[0] <= ratio <= FRONTAL_RATIO_RANGE[1]:
        return "frontal"
    return "unknown"


class AuditAccumulator:
    """Collect frames and produce the ticket's side-view pass/fail report."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._first_at: datetime | None = None
        self._last_at: datetime | None = None
        self._frames = 0
        self._pose_frames = 0
        self._ratios: list[float] = []
        self._shoulder_pcts: list[float] = []
        self._hip_visible = 0
        self._jitter_diffs: list[float] = []
        self._smoothed_5s = RollingMedian(5.0)
        self._smoothed_min: float | None = None
        self._smoothed_max: float | None = None
        self._previous_head_fwd: float | None = None

    def add(self, observation: AuditObservation) -> None:
        if self._first_at is None:
            self._first_at = observation.at
        self._last_at = observation.at
        self._frames += 1

        if not observation.detected:
            return
        self._pose_frames += 1
        if observation.ear_shoulder_ratio is not None:
            self._ratios.append(observation.ear_shoulder_ratio)
        if observation.shoulder_width_pct is not None:
            self._shoulder_pcts.append(observation.shoulder_width_pct)
        if observation.hip_visible:
            self._hip_visible += 1

        head_fwd = observation.head_fwd
        if head_fwd is None:
            return
        if self._previous_head_fwd is not None:
            self._jitter_diffs.append(abs(head_fwd - self._previous_head_fwd))
        self._previous_head_fwd = head_fwd

        smoothed = self._smoothed_5s.add(observation.at, head_fwd)
        self._smoothed_min = (
            smoothed if self._smoothed_min is None else min(self._smoothed_min, smoothed)
        )
        self._smoothed_max = (
            smoothed if self._smoothed_max is None else max(self._smoothed_max, smoothed)
        )

    def report(self, *, min_duration_seconds: float = 0.0) -> AuditReport:
        duration = 0.0
        if self._first_at is not None and self._last_at is not None:
            duration = (self._last_at - self._first_at).total_seconds()

        detection_rate = self._pose_frames / self._frames if self._frames else 0.0
        ratio = median(self._ratios) if self._ratios else None
        view = classify_view(ratio)
        shoulder_pct = median(self._shoulder_pcts) if self._shoulder_pcts else None
        hip_rate = self._hip_visible / self._pose_frames if self._pose_frames else None
        jitter = median(self._jitter_diffs) if self._jitter_diffs else 0.0
        smoothed_range = (
            self._smoothed_max - self._smoothed_min
            if self._smoothed_min is not None and self._smoothed_max is not None
            else 0.0
        )

        signal_threshold = max(
            self.settings.audit_min_signal_range_deg,
            jitter * self.settings.audit_signal_jitter_multiple,
        )
        checks = {
            "duration": duration >= min_duration_seconds,
            "view_is_side": view == "side",
            "detection_rate": detection_rate >= self.settings.audit_min_detection,
            "shoulder_width": shoulder_pct is not None
            and shoulder_pct >= self.settings.audit_min_shoulder_pct,
            "hip_visible": hip_rate is not None and hip_rate >= self.settings.audit_min_hip_visible,
            "head_fwd_stable": jitter <= self.settings.audit_max_jitter_deg,
            "head_fwd_signal": smoothed_range >= signal_threshold,
        }
        reasons: list[str] = []
        if not checks["duration"]:
            reasons.append(
                f"audit window {duration:.1f}s is shorter than "
                f"{min_duration_seconds:.1f}s; the stream dropped frames"
            )
        if view != "side":
            if ratio is None:
                reasons.append("no pose to determine camera view; a side view is required")
            else:
                reasons.append(
                    f"view is {view!r}; a side view is required (ear/shoulder ratio {ratio:.3f})"
                )
        if not checks["detection_rate"]:
            reasons.append(
                f"pose detection {detection_rate:.1%} is below "
                f"{self.settings.audit_min_detection:.0%}"
            )
        if not checks["shoulder_width"]:
            reasons.append(
                "shoulders occupy "
                + ("no frames" if shoulder_pct is None else f"{shoulder_pct:.1f}%")
                + f" of frame width; need >= {self.settings.audit_min_shoulder_pct:.1f}%"
            )
        if not checks["hip_visible"]:
            reasons.append(
                "hip visibility "
                + ("unknown" if hip_rate is None else f"{hip_rate:.0%}")
                + f" is below {self.settings.audit_min_hip_visible:.0%}"
            )
        if not checks["head_fwd_stable"]:
            reasons.append(
                f"head_fwd jitter {jitter:.2f} deg exceeds "
                f"{self.settings.audit_max_jitter_deg:.2f} deg"
            )
        if not checks["head_fwd_signal"]:
            reasons.append(
                f"5 s smoothed head_fwd range {smoothed_range:.1f} deg is too small "
                f"(need >= {signal_threshold:.1f} deg)"
            )

        return AuditReport(
            passed=all(checks.values()),
            view=view,
            duration_seconds=duration,
            frames=self._frames,
            pose_frames=self._pose_frames,
            detection_rate=detection_rate,
            ear_shoulder_ratio=ratio,
            shoulder_width_pct=shoulder_pct,
            hip_visible_rate=hip_rate,
            head_fwd_jitter_deg=jitter,
            head_fwd_range_5s_deg=smoothed_range,
            checks=checks,
            reasons=reasons,
        )
