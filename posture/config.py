"""Environment-driven configuration.

All values have safe defaults so that ``python -m posture`` can start (and
serve ``/healthz``) before a camera or a baseline exists.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .schedule import BlockWindow, DateRange, SchoolCalendar, ServiceSchedule

LOGGER = logging.getLogger(__name__)

# Threshold policy values a human may override from the dashboard after
# reviewing human-labelled reminders.  Raw metric thresholds are derived from
# these ratios/deltas and the baseline, never stored directly, so re-calibration
# remains meaningful.
THRESHOLD_OVERRIDE_KEYS: tuple[str, ...] = (
    "head_up_ratio",
    "head_fwd_drop",
    "too_close_ratio",
    "occupancy_threshold",
)

# Sanity bounds applied to both loaded overrides and newly suggested values.
# Ratios stay strictly positive and < 2, the fwd delta 0..60 degrees, and
# the occupancy duty 0.1..1.0, so a bad value can never disable judgement.
THRESHOLD_OVERRIDE_BOUNDS: dict[str, tuple[float, float]] = {
    "head_up_ratio": (0.1, 1.5),
    "head_fwd_drop": (0.0, 60.0),
    "too_close_ratio": (0.5, 2.0),
    "occupancy_threshold": (0.1, 1.0),
}


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name, "1" if default else "0").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def load_school_breaks(path: Path) -> tuple[DateRange, ...]:
    """Read winter/summer break ranges; a bad file must never stop startup.

    The file is hand-edited once per term, so a stray comma is likely.  Any
    problem degrades to "no breaks declared", which means the daytime block
    follows the statutory calendar alone.
    """
    if not path.exists():
        return ()
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("could not read school breaks from %s: %s", path, exc)
        return ()
    entries = payload.get("breaks") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        LOGGER.warning("school breaks file has no 'breaks' list: %s", path)
        return ()
    ranges: list[DateRange] = []
    for entry in entries:
        if not isinstance(entry, dict):
            LOGGER.warning("ignoring non-object school break entry: %r", entry)
            continue
        try:
            ranges.append(
                DateRange.parse(
                    str(entry.get("start", "")),
                    str(entry.get("end", "")),
                    str(entry.get("label", "")),
                )
            )
        except ValueError as exc:
            LOGGER.warning("ignoring invalid school break entry %r: %s", entry, exc)
    return tuple(ranges)


@dataclass(frozen=True)
class Settings:
    # Input
    rtsp_url: str = ""
    model_path: Path = Path("models/pose_landmarker_full.task")
    data_dir: Path = Path("data")
    target_fps: float = 5.0
    min_gray_std: float = 10.0

    # Calibration and judgement
    calibration_seconds: float = 2.0
    calibration_min_samples: int = 5
    window_seconds: float = 20.0
    window_span_tolerance: float = 1.0
    smoothing_seconds: float = 3.0
    occupancy_threshold: float = 0.55
    head_up_ratio: float = 0.50
    head_fwd_drop: float = 15.0
    too_close_ratio: float = 1.15
    no_pose_reset_seconds: float = 5.0

    # Speech gating
    cooldown_seconds: float = 300.0
    # Debug mode disables the cooldown between reminders (cooldown_seconds
    # becomes 0) to speed up threshold observation.  Production always keeps
    # at least 300 s (5 min) regardless of the configured value.  The hourly
    # cap stays unlimited in both modes.
    debug_mode: bool = False
    # A floor between reminders of *any* class, applied in every mode including
    # debug.  The per-class cooldown cannot throttle alternating classes: on
    # 2026-11-21 `too_close` and `head_down` unlocked each other and produced 50
    # spoken reminders in 30 seconds while a child sat at the desk.  Debug mode
    # still shortens the per-class cooldown to zero, but this interval keeps the
    # speaker from machine-gunning.
    min_reminder_interval_seconds: float = 30.0
    # 0 or negative means no hourly cap.  Kept configurable so a parent can
    # re-enable a ceiling later without a code change.
    hourly_cap: int = 0
    # Time-of-day policy.  Night and school hours block detection entirely;
    # late night keeps monitoring but silences the speaker.
    night_block: BlockWindow = field(default_factory=lambda: BlockWindow.parse("00:00-08:00"))
    school_block: BlockWindow = field(default_factory=lambda: BlockWindow.parse("08:00-18:00"))
    late_night_block: BlockWindow = field(
        default_factory=lambda: BlockWindow.parse("22:00-24:00")
    )
    school_breaks_path: Path = Path("data/school_breaks.json")
    calendar: SchoolCalendar = field(default_factory=SchoolCalendar)
    # Minimum gap between "detection blocked; skipped N frames" log lines.  A
    # whole school day is ~65000 skipped frames; logging each would flood the
    # journal.
    blocked_frame_log_interval: float = 300.0
    media_base_url: str = "http://192.168.2.130:8090"
    speak_cmd: str = ""
    speak_timeout: float = 10.0
    require_audit: bool = True

    # Optional alert images: only the frame that triggers a reminder, local
    # only, pruned after the retention window. Off unless explicitly enabled.
    capture_alert_images: bool = False
    alert_image_retention_hours: float = 25.0
    alert_image_max_width: int = 960
    alert_image_quality: int = 80

    # Motion presence gate: an empty chair must not be judged as a seated
    # child.  Reminders are suppressed while the scene shows no motion.
    motion_gate_enabled: bool = True
    motion_sample_interval: float = 0.5
    motion_window_seconds: float = 15.0
    motion_min_moving_frames: int = 2
    motion_change_threshold: int = 18
    motion_min_changed_ratio: float = 0.03

    # Region of interest for pose detection (normalised 0..1).  Cropping to the
    # chair is the primary false-positive control at this camera distance: desk
    # clutter produced a 72.5% false-pose rate versus 0% with a chair-only crop.
    # Values are the defaults measured for the study-desk installation; set
    # POSTURE_ROI=0,0,1,1 to disable cropping.
    roi_x1: float = 0.44
    roi_y1: float = 0.14
    roi_x2: float = 0.84
    roi_y2: float = 0.98

    # Width of the frame the stored baseline's s_norm was measured against.
    # Cropping shrinks the divisor, so the baseline must be rescaled to the crop
    # before the too_close comparison.  Set to 0 to disable rescaling (only
    # correct when no ROI crop is applied).
    source_frame_width: int = 1920

    # Camera audit
    audit_seconds: float = 20.0
    # The audit verifies the camera can *measure* this installation, not that the
# child holds still.  Measured while a child was drawing at the desk: detection
# 76.6%, jitter 4.09 deg.  The original 0.80 / 2.5 documented a controlled
# 30-second trial with a cooperative subject; used live it rejected a perfectly
# good camera.  0.65 / 6.0 leaves margin below the live measurement while still
# catching a genuinely mis-aimed or unstable camera.
    audit_min_detection: float = 0.65
    audit_min_shoulder_pct: float = 8.0
    audit_min_hip_visible: float = 0.50
    # Live measurement while a child draws at the desk: 4.53-6.44 deg depending on
# how much she moves.  The audit asks "can this camera measure?", not "is the
# child still?", so the ceiling sits above normal fidgeting: 8 deg.  Detection
# rate has been 97-100% across runs, which is the real signal that the camera is
# aimed correctly.
    audit_max_jitter_deg: float = 8.0
    audit_min_signal_range_deg: float = 5.0
    # The signal check requires the smoothed head_fwd excursion to be clearly
# larger than frame-to-frame noise: threshold = max(fixed floor, jitter * this).
# At 3.0 the requirement scales 1:1 with jitter, so a child who moves naturally
# (jitter 7.4 deg -> required 22.3 deg) could never pass, no matter how much she
# actually changes posture.  2.0 keeps the "signal must beat noise" intent while
# staying reachable for a moving child.
    # The signal check asserts the smoothed head_fwd excursion exceeds frame-to-frame
# noise: threshold = max(fixed floor, jitter * this multiple).  Any multiple
# couples the two checks -- a child who moves more raises the bar she must clear.
# At 2.0 a 7.47 deg jitter demanded 14.9 deg of excursion and failed a run that
# actually moved 14.6 deg.  1.5 keeps "signal must beat noise" without the
# self-defeating spiral.
    audit_signal_jitter_multiple: float = 1.5
    auto_audit: bool = True
    auto_audit_presence_seconds: float = 10.0
    auto_audit_retry_seconds: float = 600.0

    # HTTP API
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    @classmethod
    def from_env(cls) -> Settings:
        debug_mode = _env_bool("POSTURE_DEBUG_MODE", False)
        configured_cooldown = _env_float("POSTURE_COOLDOWN_SECONDS", 300.0)
        cooldown = 0.0 if debug_mode else max(300.0, configured_cooldown)
        settings = cls(
            debug_mode=debug_mode,
            cooldown_seconds=cooldown,
            min_reminder_interval_seconds=_env_float(
                "POSTURE_MIN_REMINDER_INTERVAL", 30.0
            ),
            rtsp_url=_env_str("POSTURE_RTSP_URL", ""),
            model_path=Path(_env_str("POSTURE_MODEL_PATH", "models/pose_landmarker_full.task")),
            data_dir=Path(_env_str("POSTURE_DATA_DIR", "data")),
            target_fps=_env_float("POSTURE_TARGET_FPS", 5.0),
            min_gray_std=_env_float("POSTURE_MIN_GRAY_STD", 10.0),
            calibration_seconds=_env_float("POSTURE_CALIBRATION_SECONDS", 2.0),
            calibration_min_samples=_env_int("POSTURE_CALIBRATION_MIN_SAMPLES", 5),
            window_seconds=_env_float("POSTURE_WINDOW_SECONDS", 20.0),
            window_span_tolerance=_env_float("POSTURE_WINDOW_SPAN_TOLERANCE", 1.0),
            smoothing_seconds=_env_float("POSTURE_SMOOTHING_SECONDS", 3.0),
            occupancy_threshold=_env_float("POSTURE_OCCUPANCY_THRESHOLD", 0.55),
            head_up_ratio=_env_float("POSTURE_HEAD_UP_RATIO", 0.50),
            head_fwd_drop=_env_float("POSTURE_HEAD_FWD_DROP", 15.0),
            too_close_ratio=_env_float("POSTURE_TOO_CLOSE_RATIO", 1.15),
            no_pose_reset_seconds=_env_float("POSTURE_NO_POSE_RESET_SECONDS", 5.0),
            hourly_cap=_env_int("POSTURE_HOURLY_CAP", 0),
            night_block=BlockWindow.parse(_env_str("POSTURE_NIGHT_BLOCK", "00:00-08:00")),
            school_block=BlockWindow.parse(_env_str("POSTURE_SCHOOL_BLOCK", "08:00-18:00")),
            late_night_block=BlockWindow.parse(
                _env_str("POSTURE_LATE_NIGHT_BLOCK", "22:00-24:00")
            ),
            school_breaks_path=Path(
                _env_str("POSTURE_SCHOOL_BREAKS", "data/school_breaks.json")
            ),
            blocked_frame_log_interval=_env_float("POSTURE_BLOCKED_LOG_INTERVAL", 300.0),
            media_base_url=_env_str("POSTURE_MEDIA_BASE_URL", "http://192.168.2.130:8090"),
            speak_cmd=_env_str("SPEAK_CMD", ""),
            speak_timeout=_env_float("POSTURE_SPEAK_TIMEOUT", 10.0),
            require_audit=_env_bool("POSTURE_REQUIRE_AUDIT", True),
            capture_alert_images=_env_bool("POSTURE_ALERT_IMAGES", False),
            alert_image_retention_hours=_env_float("POSTURE_ALERT_IMAGE_RETENTION_HOURS", 25.0),
            alert_image_max_width=_env_int("POSTURE_ALERT_IMAGE_MAX_WIDTH", 960),
            alert_image_quality=_env_int("POSTURE_ALERT_IMAGE_QUALITY", 80),
            motion_gate_enabled=_env_bool("POSTURE_MOTION_GATE", True),
            motion_sample_interval=_env_float("POSTURE_MOTION_SAMPLE_INTERVAL", 0.5),
            motion_window_seconds=_env_float("POSTURE_MOTION_WINDOW_SECONDS", 15.0),
            motion_min_moving_frames=_env_int("POSTURE_MOTION_MIN_MOVING", 2),
            motion_change_threshold=_env_int("POSTURE_MOTION_CHANGE_THRESHOLD", 18),
            motion_min_changed_ratio=_env_float("POSTURE_MOTION_MIN_RATIO", 0.03),
            roi_x1=_env_float("POSTURE_ROI_X1", 0.44),
            roi_y1=_env_float("POSTURE_ROI_Y1", 0.14),
            roi_x2=_env_float("POSTURE_ROI_X2", 0.84),
            roi_y2=_env_float("POSTURE_ROI_Y2", 0.98),
            source_frame_width=_env_int("POSTURE_SOURCE_FRAME_WIDTH", 1920),
            audit_seconds=_env_float("POSTURE_AUDIT_SECONDS", 20.0),
            audit_min_detection=_env_float("POSTURE_AUDIT_MIN_DETECTION", 0.65),
            audit_min_shoulder_pct=_env_float("POSTURE_AUDIT_MIN_SHOULDER_PCT", 8.0),
            audit_min_hip_visible=_env_float("POSTURE_AUDIT_MIN_HIP_VISIBLE", 0.50),
            audit_max_jitter_deg=_env_float("POSTURE_AUDIT_MAX_JITTER_DEG", 8.0),
            audit_min_signal_range_deg=_env_float("POSTURE_AUDIT_MIN_SIGNAL_RANGE_DEG", 5.0),
            audit_signal_jitter_multiple=_env_float("POSTURE_AUDIT_SIGNAL_JITTER_MULTIPLE", 1.5),
            api_host=_env_str("POSTURE_API_HOST", "0.0.0.0"),
            api_port=_env_int("POSTURE_API_PORT", 8080),
        )
        breaks = load_school_breaks(settings.school_breaks_path)
        settings = replace(settings, calendar=SchoolCalendar(breaks=breaks))
        LOGGER.info(
            "schedule: night=%s school=%s late_night=%s breaks=%s",
            settings.night_block,
            settings.school_block,
            settings.late_night_block,
            [f"{entry.start}~{entry.end}" for entry in breaks],
        )
        return cls._with_threshold_overrides(settings)

    @property
    def schedule(self) -> ServiceSchedule:
        # Cheap to rebuild (a few frozen dataclasses), and it deliberately
        # reuses the single ``self.calendar`` instance so the out-of-range
        # warning de-duplicates across calls.  Building a fresh SchoolCalendar
        # here would log on every /healthz poll.
        return ServiceSchedule(
            night=self.night_block,
            school=self.school_block,
            late_night=self.late_night_block,
            calendar=self.calendar,
        )

    @classmethod
    def _with_threshold_overrides(cls, settings: Settings) -> Settings:
        """Layer a human-approved thresholds_override.json on top of env.

        The file is optional and shaped as ``{"values": {key: number}}``.
        Only keys in :data:`THRESHOLD_OVERRIDE_KEYS` are honoured; malformed or
        unusable values are ignored with a warning so a bad file can never
        prevent the service from starting.
        """
        path = settings.data_dir / "thresholds_override.json"
        if not path.exists():
            return settings
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("could not read %s: %s", path, exc)
            return settings
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            LOGGER.warning("threshold override has no 'values' object: %s", path)
            return settings
        updates: dict[str, float] = {}
        for key in THRESHOLD_OVERRIDE_KEYS:
            if key not in values:
                continue
            try:
                number = float(values[key])
            except (TypeError, ValueError):
                LOGGER.warning("ignoring non-numeric threshold override %s=%r", key, values[key])
                continue
            low, high = THRESHOLD_OVERRIDE_BOUNDS[key]
            if not (low <= number <= high):
                LOGGER.warning(
                    "ignoring out-of-range threshold override %s=%r (allowed %s..%s)",
                    key, values[key], low, high,
                )
                continue
            updates[key] = number
        if not updates:
            return settings
        LOGGER.info("applying threshold overrides from %s: %s", path, updates)
        return replace(settings, **updates)

    @property
    def baseline_path(self) -> Path:
        return self.data_dir / "baseline.json"

    @property
    def thresholds_override_path(self) -> Path:
        return self.data_dir / "thresholds_override.json"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "posture.sqlite3"

    @property
    def alert_image_dir(self) -> Path:
        return self.data_dir / "alerts"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def min_window_span_seconds(self) -> float:
        return max(0.0, self.window_seconds - self.window_span_tolerance)

    @property
    def frame_interval_seconds(self) -> float:
        return 1.0 / self.target_fps if self.target_fps > 0 else 0.2
