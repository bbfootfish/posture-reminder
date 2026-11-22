"""The long-running service: RTSP capture, judgement, speech, HTTP state."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import time as datetime_time
from typing import Any

from .alerts import prune_alert_images, save_alert_image
from .audit import AuditAccumulator, AuditObservation, AuditReport
from .baseline import (
    BaselineError,
    BaselineStore,
    CalibrationResult,
    CalibrationSession,
)
from .capture import RtspSource, backoff_delay
from .config import Settings
from .errors import CalibrationTimeout, ServiceBusy, ServiceUnavailable
from .frames import frame_is_usable
from .judge import thresholds_for
from .metrics import PoseDiagnostics, diagnostics_from_landmarks, extract_sample
from .motion import MotionDetector
from .roi import Roi, crop_with_size
from .models import ISSUES, Baseline, Reminder
from .monitor import MonitorHistory
from .persistence import MissedEventStore, ReminderStore, VERDICTS
from .pose import PoseEstimator
from .presence import PresenceTimer
from .schedule import Mode
from .speaker import Speaker
from .state_machine import ReminderStateMachine
from .tuning import suggest as suggest_thresholds

LOGGER = logging.getLogger(__name__)


def _coerce_float(value: Any) -> float | None:
    """Best-effort float for optional snapshot values."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


class PostureService:
    """Owns all mutable runtime state.

    The capture loop runs in the main thread; ``/healthz``, ``/calibrate``
    and ``/audit`` are served from daemon HTTP threads and share state through
    ``self._lock``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        clock=datetime.now,
        speaker: Speaker | None = None,
        store: ReminderStore | None = None,
        missed_store: MissedEventStore | None = None,
    ) -> None:
        self.settings = settings
        settings.ensure_data_dir()
        self._clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()

        self._baseline_store = BaselineStore(settings.baseline_path)
        self._baseline: Baseline | None = None
        self._baseline_error: str | None = None
        try:
            self._baseline = self._baseline_store.load()
        except BaselineError as exc:
            self._baseline_error = str(exc)
            LOGGER.error("baseline could not be loaded: %s", exc)

        self._engine: ReminderStateMachine | None = (
            ReminderStateMachine(self._baseline, settings) if self._baseline is not None else None
        )
        self._store = store or ReminderStore(settings.database_path)
        self._missed = missed_store or MissedEventStore(settings.database_path)
        self._speaker = speaker or Speaker(settings)
        self._monitor = MonitorHistory(settings.database_path)
        self._motion = MotionDetector(
            sample_interval=settings.motion_sample_interval,
            window_seconds=settings.motion_window_seconds,
            min_moving_frames=settings.motion_min_moving_frames,
            change_threshold=settings.motion_change_threshold,
            min_changed_ratio=settings.motion_min_changed_ratio,
        )
        self._motion_active: bool = True
        self._roi = Roi(
            x1=settings.roi_x1,
            y1=settings.roi_y1,
            x2=settings.roi_x2,
            y2=settings.roi_y2,
        )
        LOGGER.info("pose ROI: %s", self._roi.as_dict())
        if settings.capture_alert_images:
            try:
                settings.alert_image_dir.mkdir(parents=True, exist_ok=True)
                removed = prune_alert_images(
                    settings.alert_image_dir,
                    retention_hours=settings.alert_image_retention_hours,
                    now=self._clock(),
                )
                if removed:
                    LOGGER.info("pruned %s old alert images", removed)
            except OSError:
                LOGGER.exception("could not prepare the alert image directory")

        self._pose: PoseEstimator | None = None
        self._model_error: str | None = None
        self._source: RtspSource | None = None
        self._api = None

        self._stream_connected = False
        self._last_frame_at: datetime | None = None
        self._last_sample_at: datetime | None = None
        self._last_reminder: Reminder | None = None
        self._last_error: str | None = None
        self._skipped_gray = 0
        self._last_mode: Mode = Mode.ALLOW
        self._skipped_blocked = 0
        self._last_blocked_log_at: datetime | None = None
        self._processed_monotonic: deque[float] = deque()

        self._calibration: CalibrationSession | None = None
        self._calibration_done: threading.Event | None = None
        self._calibration_error: str | None = None
        self._calibration_result: CalibrationResult | None = None

        self._audit: AuditAccumulator | None = None
        self._audit_done: threading.Event | None = None
        self._audit_started_at: datetime | None = None
        self._audit_report: AuditReport | None = None
        self._audit_finished_at: datetime | None = None
        self._audit_passed: bool | None = None
        self._last_audit_attempt_at: datetime | None = None
        self._presence = PresenceTimer(settings.auto_audit_presence_seconds)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start_api(self) -> int:
        from .api import ApiServer

        self._api = ApiServer(self, self.settings.api_host, self.settings.api_port)
        port = self._api.start()
        LOGGER.info("HTTP API listening on %s:%s", self.settings.api_host, port)
        return port

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except ValueError:  # not the main thread (tests)
            pass

        if not self.settings.rtsp_url:
            LOGGER.warning("POSTURE_RTSP_URL is not configured; serving health/API only")
            self._wait_for_stop()
            return

        if not self._ensure_pose_model():
            self._wait_for_stop()
            return

        attempt = 0
        while not self._stop.is_set():
            source = RtspSource(self.settings.rtsp_url)
            with self._lock:
                self._source = source
            if not source.open():
                with self._lock:
                    self._source = None
                    self._stream_connected = False
                delay = backoff_delay(attempt)
                attempt += 1
                LOGGER.warning("RTSP connect failed; retrying in %ss", delay)
                self._stop.wait(delay)
                continue

            got_frame = self._consume_stream(source)
            source.close()
            with self._lock:
                self._source = None
                self._stream_connected = False
                if self._audit is not None and self._audit_done is None:
                    # An automatic audit has no HTTP waiter to time it out.
                    self._audit = None
                    self._audit_passed = False
                    LOGGER.warning("automatic camera audit cancelled: stream disconnected")
            if self._stop.is_set():
                break
            attempt = 0 if got_frame else attempt + 1
            delay = backoff_delay(attempt)
            LOGGER.warning("RTSP disconnected; reconnecting in %ss", delay)
            self._stop.wait(delay)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            source = self._source
            pose = self._pose
            api = self._api
        if source is not None:
            source.close()
        if pose is not None:
            pose.close()
        if api is not None:
            api.stop()
        try:
            self._store.close()
        except Exception:  # pragma: no cover - best effort shutdown
            LOGGER.debug("store close failed", exc_info=True)
        try:
            self._monitor.close()
        except Exception:  # pragma: no cover - best effort shutdown
            LOGGER.debug("monitor close failed", exc_info=True)

    def request_stop(self) -> None:
        self._stop.set()

    def _on_signal(self, signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s; stopping", signum)
        self._stop.set()

    def _wait_for_stop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(1.0)

    def _ensure_pose_model(self) -> bool:
        try:
            self._pose = PoseEstimator(self.settings.model_path)
        except Exception as exc:  # keep /healthz alive when native libs are missing
            self._model_error = f"pose model unavailable: {exc}"
            self._last_error = self._model_error
            LOGGER.error("pose model unavailable: %s", exc)
            return False
        LOGGER.info("pose model loaded: %s", self.settings.model_path)
        return True

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------
    def _consume_stream(self, source: RtspSource) -> bool:
        got_frame = False
        next_due = 0.0
        first_frame_deadline = time.monotonic() + 15.0

        while not self._stop.is_set():
            frame = source.read()
            monotonic_now = time.monotonic()
            if frame is None:
                if not got_frame and monotonic_now < first_frame_deadline:
                    time.sleep(0.05)
                    continue
                if got_frame:
                    LOGGER.warning("RTSP stream ended")
                else:
                    LOGGER.warning("RTSP first frame timeout")
                break

            got_frame = True
            now = self._clock()
            with self._lock:
                self._last_frame_at = now
                if not self._stream_connected:
                    LOGGER.info("RTSP stream producing frames")
                self._stream_connected = True
                self._last_error = None

            height, width = frame.shape[:2]
            if not frame_is_usable(frame, self.settings.min_gray_std):
                with self._lock:
                    self._skipped_gray += 1
                continue

            # Manual operations (calibrate / audit) must work at any hour —
            # otherwise the camera audit could never be re-run during the day,
            # when the child is at school and the desk is empty.
            with self._lock:
                collecting = self._calibration is not None or self._audit is not None

            mode = self.settings.schedule.mode(now)
            with self._lock:
                self._last_mode = mode
            if mode is Mode.BLOCK and not collecting:
                # Keep reading so the RTSP buffer never builds up; inference and
                # storage are what we actually save.  A silent 18-hour gap would
                # otherwise be indistinguishable from a dead capture thread.
                with self._lock:
                    self._skipped_blocked += 1
                if (
                    self._last_blocked_log_at is None
                    or (now - self._last_blocked_log_at).total_seconds()
                    >= self.settings.blocked_frame_log_interval
                ):
                    self._last_blocked_log_at = now
                    LOGGER.info(
                        "detection blocked (%s); skipped %s frames so far",
                        self.settings.schedule.explain(now)["reason"],
                        self._skipped_blocked,
                    )
                continue

            # Track scene motion independently of the pose model.  An empty
            # chair produces high-confidence false poses; a lack of motion is
            # the most reliable signal that no one is actually seated.
            if self.settings.motion_gate_enabled:
                self._motion.update(frame, now)

            if not collecting:
                if monotonic_now < next_due:
                    continue
                next_due = monotonic_now + self.settings.frame_interval_seconds

            sample, diagnostics = self._detect(frame, width, height)
            with self._lock:
                self._record_processed(monotonic_now)
                if sample is not None:
                    self._last_sample_at = now

                reminder: Reminder | None = None
                if self._calibration is not None:
                    self._feed_calibration(sample, now)
                elif self._audit is not None or self._maybe_start_auto_audit(sample, now):
                    self._feed_audit(diagnostics, now)
                else:
                    reminder = self._observe(sample, now)

            if reminder is not None:
                self._deliver(reminder, frame)
        return got_frame

    def _detect(self, frame: Any, width: int, height: int) -> tuple[Any | None, PoseDiagnostics]:
        if self._pose is None:
            return None, PoseDiagnostics.not_detected()
        # Crop to the configured region before inference.  At this camera
        # distance the pose model hallucinates skeletons on desk clutter, and
        # a chair-only crop removes them entirely (72.5% -> 0% over 40 frames).
        # The cropped dimensions must feed extract_sample: its shoulder-width
        # gate is frame-relative, so passing the full frame size would reject
        # every real subject once cropping shrinks the picture.
        sub, (crop_w, crop_h) = crop_with_size(frame, self._roi)
        try:
            landmarks = self._pose.detect(sub, int(time.monotonic() * 1000))
        except Exception as exc:  # keep the stream alive on one bad frame
            LOGGER.exception("pose inference failed")
            with self._lock:
                self._last_error = f"pose inference failed: {exc}"
            return None, PoseDiagnostics.not_detected()
        return (
            extract_sample(landmarks, crop_w, crop_h),
            diagnostics_from_landmarks(landmarks, crop_w, crop_h),
        )

    def _record_processed(self, monotonic_now: float) -> None:
        self._processed_monotonic.append(monotonic_now)
        cutoff = monotonic_now - 5.0
        while self._processed_monotonic and self._processed_monotonic[0] < cutoff:
            self._processed_monotonic.popleft()

    def _maybe_start_auto_audit(self, sample, now: datetime) -> bool:
        """Run the camera audit once a person has been seated for a while.

        The child often arrives after the service starts.  Waiting for a valid
        pose for ``auto_audit_presence_seconds`` avoids a manual call after
        every startup.  ``head_up > 0`` keeps a person lying on the desk/sofa
        from triggering the audit immediately.
        """
        if not self.settings.auto_audit or not self.settings.require_audit:
            return False
        if self._audit_passed is True:
            return False
        if self._calibration is not None or self._audit is not None:
            return False
        if self._last_audit_attempt_at is not None:
            elapsed = (now - self._last_audit_attempt_at).total_seconds()
            if elapsed < self.settings.auto_audit_retry_seconds:
                return False
        seated = sample is not None and sample.metrics.head_up > 0.0
        if not self._presence.update(seated, now):
            return False
        self._begin_audit(now, automatic=True)
        return True

    def current_mode(self, at: datetime | None = None) -> Mode:
        """Resolve the schedule for ``at`` (defaults to now)."""
        moment = self._clock() if at is None else at
        return self.settings.schedule.mode(moment)

    def _observe(self, sample, now: datetime) -> Reminder | None:
        if self._engine is None:
            return None
        mode = self.settings.schedule.mode(now)
        motion_active = (
            not self.settings.motion_gate_enabled
        ) or self._motion.active(now)
        with self._lock:
            self._motion_active = motion_active
        audit_ok = (not self.settings.require_audit) or self._audit_passed is True
        allow = audit_ok and motion_active
        reminder = self._engine.observe(
            None if sample is None else sample.metrics,
            now,
            mode=mode,
            allow_reminder=allow,
        )
        self._monitor.maybe_add(
            now,
            self._engine.snapshot(now, mode=mode),
            person=sample is not None,
        )
        if reminder is not None:
            self._last_reminder = reminder
        return reminder

    def _deliver(self, reminder: Reminder, frame: Any | None = None) -> None:
        # _observe already passes allow_reminder=False during MONITOR_ONLY, so
        # the engine should never hand us a reminder then.  Reaching here would
        # mean a gate leaked; refuse to speak and log loudly rather than rely on
        # a silent second guard that would hide the bug.
        if self.settings.schedule.mode(reminder.at) is Mode.MONITOR_ONLY:
            LOGGER.error(
                "BUG: reminder escaped the late-night soft block at %s; not speaking",
                reminder.at,
            )
            return
        if self.settings.capture_alert_images and frame is not None:
            image = self._save_alert_image(reminder, frame)
            if image is not None:
                reminder = replace(reminder, image=image)
        LOGGER.info(
            "reminder: class=%s variant=%s text=%s metrics=%s image=%s",
            reminder.issue,
            reminder.variant,
            reminder.text,
            reminder.metrics.as_dict(),
            reminder.image,
        )
        self._speaker.speak(reminder)
        try:
            self._store.record(reminder)
        except Exception:  # pragma: no cover - log must never kill capture
            LOGGER.exception("could not record reminder in SQLite")
        with self._lock:
            self._last_reminder = reminder

    def _save_alert_image(self, reminder: Reminder, frame: Any) -> str | None:
        stem = f"{reminder.at.strftime('%Y%m%dT%H%M%S')}-{reminder.issue}"
        path = self.settings.alert_image_dir / f"{stem}.jpg"
        suffix = 1
        while path.exists():
            path = self.settings.alert_image_dir / f"{stem}-{suffix}.jpg"
            suffix += 1
        if not save_alert_image(
            frame,
            path,
            max_width=self.settings.alert_image_max_width,
            quality=self.settings.alert_image_quality,
        ):
            return None
        try:
            prune_alert_images(
                self.settings.alert_image_dir,
                retention_hours=self.settings.alert_image_retention_hours,
                now=reminder.at,
            )
        except OSError:
            LOGGER.debug("could not prune alert images", exc_info=True)
        return path.name

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------
    def _feed_calibration(self, sample, now: datetime) -> None:
        session = self._calibration
        if session is None:
            return
        completed = session.add(None if sample is None else sample.metrics, now)
        if not completed:
            return
        self._calibration = None
        event = self._calibration_done
        try:
            result = session.result()
        except BaselineError as exc:
            self._calibration_error = str(exc)
            LOGGER.error("calibration failed: %s", exc)
        else:
            try:
                self._baseline_store.save(result.baseline, samples=result.samples)
            except BaselineError as exc:
                self._calibration_error = str(exc)
                LOGGER.error("calibration failed: %s", exc)
            else:
                self._baseline = result.baseline
                self._engine = ReminderStateMachine(result.baseline, self.settings)
                self._calibration_result = result
                self._baseline_error = None
                LOGGER.info(
                    "calibration done: samples=%s baseline=%s",
                    result.samples,
                    result.baseline.as_dict(),
                )
        if event is not None:
            event.set()

    def calibrate(self) -> dict[str, Any]:
        with self._lock:
            if self._calibration is not None or self._audit is not None:
                raise ServiceBusy("another calibration or audit is running")
            if not self.settings.rtsp_url or self._pose is None:
                raise ServiceUnavailable(
                    "camera stream is not configured or the pose model is not loaded"
                )
            session = CalibrationSession(
                self.settings.calibration_seconds,
                self.settings.calibration_min_samples,
            )
            event = threading.Event()
            self._presence.reset()
            self._calibration = session
            self._calibration_done = event
            self._calibration_error = None
            self._calibration_result = None

        timeout = self.settings.calibration_seconds + 10.0
        finished = event.wait(timeout)
        with self._lock:
            if self._calibration is session:
                self._calibration = None
            if self._calibration_done is event:
                self._calibration_done = None
            error = self._calibration_error
            result = self._calibration_result
        if not finished:
            raise CalibrationTimeout(
                f"no calibration result within {timeout:.0f}s; is the camera streaming?"
            )
        if error is not None:
            raise BaselineError(error)
        if result is None:
            raise CalibrationTimeout("calibration finished without a result")
        return {
            "ok": True,
            "baseline": result.baseline.as_dict(),
            "samples": result.samples,
            "started_at": result.started_at.isoformat(timespec="seconds"),
            "finished_at": result.finished_at.isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------------
    # Camera audit
    # ------------------------------------------------------------------
    def _begin_audit(
        self, now: datetime, *, automatic: bool
    ) -> tuple[AuditAccumulator, threading.Event | None]:
        accumulator = AuditAccumulator(self.settings)
        event = None if automatic else threading.Event()
        self._audit = accumulator
        self._audit_done = event
        self._audit_started_at = now
        self._audit_report = None
        self._audit_passed = None
        self._last_audit_attempt_at = now
        self._presence.reset()
        LOGGER.info("camera audit started (%s)", "automatic" if automatic else "manual")
        return accumulator, event

    def _feed_audit(self, diagnostics: PoseDiagnostics, now: datetime) -> None:
        accumulator = self._audit
        if accumulator is None:
            return
        accumulator.add(AuditObservation.from_diagnostics(now, diagnostics))
        started = self._audit_started_at or now
        if (now - started).total_seconds() < self.settings.audit_seconds:
            return
        self._finalize_audit(now)

    def _finalize_audit(self, now: datetime) -> None:
        accumulator = self._audit
        if accumulator is None:
            return
        report = accumulator.report(min_duration_seconds=self.settings.audit_seconds * 0.8)
        self._audit = None
        self._audit_report = report
        self._audit_finished_at = now
        self._audit_passed = report.passed
        LOGGER.info("camera audit: %s", report.as_dict())
        if not report.passed:
            LOGGER.warning(
                "camera audit failed; reminders stay disabled: %s",
                "; ".join(report.reasons),
            )
        event = self._audit_done
        if event is not None:
            event.set()

    def audit(self) -> dict[str, Any]:
        with self._lock:
            if self._calibration is not None or self._audit is not None:
                raise ServiceBusy("another calibration or audit is running")
            if not self.settings.rtsp_url or self._pose is None:
                raise ServiceUnavailable(
                    "camera stream is not configured or the pose model is not loaded"
                )
            accumulator, event = self._begin_audit(self._clock(), automatic=False)

        assert event is not None
        timeout = self.settings.audit_seconds + 15.0
        finished = event.wait(timeout)
        with self._lock:
            if self._audit is accumulator:
                self._audit = None
            if self._audit_done is event:
                self._audit_done = None
            report = self._audit_report
        if not finished:
            raise CalibrationTimeout(
                f"camera audit did not finish within {timeout:.0f}s; is the camera streaming?"
            )
        if report is None:
            raise CalibrationTimeout("camera audit finished without a report")
        return report.as_dict()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            monotonic_now = time.monotonic()
            times = self._processed_monotonic
            cutoff = monotonic_now - 5.0
            while times and times[0] < cutoff:
                times.popleft()
            fps = 0.0
            if len(times) >= 2:
                span = times[-1] - times[0]
                fps = (len(times) - 1) / span if span > 0 else 0.0

            stream_age = (
                None if self._last_frame_at is None else (now - self._last_frame_at).total_seconds()
            )
            schedule_info = self.settings.schedule.explain(now)
            connected = self._stream_connected and (stream_age is not None and stream_age < 5.0)
            person = (
                self._last_sample_at is not None
                and (now - self._last_sample_at).total_seconds()
                < self.settings.no_pose_reset_seconds
            )

            if not self.settings.rtsp_url:
                state = "unconfigured"
            elif self._model_error is not None:
                state = "model_error"
            elif self._calibration is not None:
                state = "calibrating"
            elif self._audit is not None:
                state = "auditing"
            elif not connected:
                state = "stream_down"
            elif self._baseline is None:
                state = "no_baseline"
            elif self.settings.require_audit and self._audit_passed is not True:
                state = "audit_required"
            elif self._engine is not None:
                state = self._engine.status
            else:
                state = "starting"

            midnight = datetime.combine(now.date(), datetime_time.min)
            try:
                reminders_today = self._store.count_since(midnight)
            except Exception:  # pragma: no cover - health must stay available
                reminders_today = 0

            healthy = (
                bool(self.settings.rtsp_url)
                and connected
                and self._baseline is not None
                and self._model_error is None
                and (not self.settings.require_audit or self._audit_passed is True)
            )
            return {
                "status": "ok" if healthy else "degraded",
                "version": "0.1.0",
                "fps": round(fps, 2),
                "person": person,
                "state": state,
                "reminders_today": reminders_today,
                "camera_audit_passed": self._audit_passed,
                "auto_audit": self.settings.auto_audit,
                "last_audit_attempt_at": (
                    None
                    if self._last_audit_attempt_at is None
                    else self._last_audit_attempt_at.isoformat(timespec="seconds")
                ),
                "camera_audit": (
                    None
                    if self._audit_report is None
                    else {
                        **self._audit_report.as_dict(),
                        "at": (
                            self._audit_finished_at.isoformat(timespec="seconds")
                            if self._audit_finished_at
                            else None
                        ),
                    }
                ),
                "baseline_present": self._baseline is not None,
                "baseline": (None if self._baseline is None else self._baseline.as_dict()),
                "stream_connected": connected,
                "last_frame_age_seconds": (None if stream_age is None else round(stream_age, 2)),
                "work_hours": self.settings.schedule.summary(),
                "schedule_mode": schedule_info["mode"],
                "schedule_reason": schedule_info["reason"],
                "next_allow_at": schedule_info["next_allow_at"],
                "next_change_at": schedule_info["next_change_at"],
                "school_day": schedule_info["school_day"],
                "holiday_name": schedule_info["holiday_name"],
                "break_label": schedule_info["break_label"],
                "calendar_ok": schedule_info["calendar_ok"],
                "calendar_supported_years": schedule_info["calendar_supported_years"],
                "skipped_blocked_frames": self._skipped_blocked,
                "calibrating": self._calibration is not None,
                "auditing": self._audit is not None,
                "skipped_gray_frames": self._skipped_gray,
                "speaking_enabled": bool(self.settings.speak_cmd),
                "alert_images_enabled": bool(self.settings.capture_alert_images),
                "motion_gate_enabled": bool(self.settings.motion_gate_enabled),
                "motion_active": bool(self._motion_active),
                "require_audit": self.settings.require_audit,
                "last_reminder": (
                    None if self._last_reminder is None else self._last_reminder.as_dict()
                ),
                "error": self._last_error or self._model_error or self._baseline_error,
            }

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    def monitor(
        self,
        *,
        minutes: float = 15.0,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the read-only dashboard payload.

        The current decision comes from the same state machine snapshot that
        the capture loop just used; ``since`` lets a polling page fetch only
        new timeline points.
        """
        with self._lock:
            now = self._clock()
            status = self.status()
            baseline = self._baseline
            snapshot = None if self._engine is None else self._engine.snapshot(now)
            series = self._monitor.series(now=now, minutes=minutes, since=since)
            midnight = datetime.combine(now.date(), datetime_time.min)
            try:
                reminders = self._store.recent_since(midnight, 100)
            except Exception:  # pragma: no cover - dashboard must stay available
                LOGGER.exception("could not read reminder history")
                reminders = []

        scoring: dict[str, Any] | None = None
        if baseline is not None:
            scoring = {
                "baseline": baseline.as_dict(),
                "policy": {
                    "head_up_ratio": self.settings.head_up_ratio,
                    "head_fwd_drop": self.settings.head_fwd_drop,
                    "too_close_ratio": self.settings.too_close_ratio,
                },
            }
            if snapshot is not None:
                scoring.update(snapshot)
            else:  # pragma: no cover - a baseline always has an engine
                scoring["status"] = "starting"
                scoring["thresholds"] = thresholds_for(
                    baseline,
                    head_up_ratio=self.settings.head_up_ratio,
                    head_fwd_drop=self.settings.head_fwd_drop,
                    too_close_ratio=self.settings.too_close_ratio,
                )
            scoring["motion_active"] = bool(self._motion_active)
            scoring["motion_gate_enabled"] = bool(self.settings.motion_gate_enabled)

        return {
            "now": now.isoformat(timespec="seconds"),
            "service": status,
            "scoring": scoring,
            "series": series,
            "reminders": reminders,
        }

    def history(
        self,
        *,
        hours: float = 24.0,
        bucket_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Aggregate the last ``hours`` into fixed buckets for the 24h view."""
        with self._lock:
            now = self._clock()
            buckets = self._monitor.buckets(
                now=now,
                hours=hours,
                bucket_seconds=bucket_seconds,
            )
            try:
                reminders = self._store.recent_since(
                    now - timedelta(hours=hours),
                    500,
                )
            except Exception:  # pragma: no cover - dashboard must stay available
                LOGGER.exception("could not read reminder history")
                reminders = []
            scoring = None
            if self._baseline is not None:
                scoring = {
                    "baseline": self._baseline.as_dict(),
                    "thresholds": thresholds_for(
                        self._baseline,
                        head_up_ratio=self.settings.head_up_ratio,
                        head_fwd_drop=self.settings.head_fwd_drop,
                        too_close_ratio=self.settings.too_close_ratio,
                    ),
                }

        samples = sum(bucket["samples"] for bucket in buckets)
        outliers = sum(bucket["outliers"] for bucket in buckets)
        present = sum(bucket["person"] for bucket in buckets)
        return {
            "now": now.isoformat(timespec="seconds"),
            "hours": hours,
            "bucket_seconds": bucket_seconds,
            "buckets": buckets,
            "reminders": reminders,
            "scoring": scoring,
            "alert_images_enabled": bool(self.settings.capture_alert_images),
            "summary": {
                "samples": samples,
                "outliers": outliers,
                "outlier_ratio": round(outliers / samples, 3) if samples else 0.0,
                "person_samples": present,
                "reminders": len(reminders),
            },
        }

    # ------------------------------------------------------------------
    # Human verdicts and threshold tuning (review-gated)
    # ------------------------------------------------------------------
    def set_verdict(
        self,
        reminder_id: int,
        verdict: str | None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record/change/clear a human verdict on a reminder.

        Returns the updated reminder row.  Raises ``ValueError`` for an
        invalid verdict; ``KeyError`` when the id does not exist.
        """
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(f"invalid verdict: {verdict!r}")
        row = self._store.set_verdict(reminder_id, verdict, note)
        if row is None:
            raise KeyError(f"no reminder with id {reminder_id}")
        LOGGER.info("verdict set: id=%s verdict=%s", reminder_id, verdict)
        return dict(row)

    def timeline(
        self,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: float = 60.0,
    ) -> list[dict[str, Any]]:
        """Aggregate an explicit window for the interactive look-back chart.

        Callers zoom by shrinking the window and the bucket size; a 1-second
        bucket at high zoom exposes individual samples.
        """
        with self._lock:
            return self._monitor.range_buckets(
                start=start, end=end, bucket_seconds=bucket_seconds
            )

    def tuning_suggestion(self) -> dict[str, Any]:
        """Compute threshold suggestions from human-labelled reminders."""
        with self._lock:
            baseline = self._baseline
        if baseline is None:
            return {
                "error": "no_baseline",
                "note": "缺少校准基线，无法计算阈值建议。",
                "reasons": [],
                "changes": {},
                "confidence": "low",
            }
        stats = self._store.verdict_stats()
        missed_stats = self._missed.stats()
        policy = {
            "head_up_ratio": self.settings.head_up_ratio,
            "head_fwd_drop": self.settings.head_fwd_drop,
            "too_close_ratio": self.settings.too_close_ratio,
            "occupancy_threshold": self.settings.occupancy_threshold,
        }
        result = suggest_thresholds(stats, baseline, policy, missed_stats=missed_stats)
        result["override_active"] = self.settings.thresholds_override_path.exists()
        return result

    def active_policy(self) -> dict[str, Any]:
        """Return the threshold policy currently in force and its source."""
        return {
            "head_up_ratio": self.settings.head_up_ratio,
            "head_fwd_drop": self.settings.head_fwd_drop,
            "too_close_ratio": self.settings.too_close_ratio,
            "occupancy_threshold": self.settings.occupancy_threshold,
            "override_active": self.settings.thresholds_override_path.exists(),
            "override_path": str(self.settings.thresholds_override_path),
        }

    def apply_thresholds(self, values: dict[str, Any]) -> dict[str, Any]:
        """Persist human-approved threshold values to thresholds_override.json.

        Values are validated against the same bounds used when loading.  The
        change takes effect on next service start; the caller is told to
        restart.  An empty ``values`` dict clears the override (revert to env).
        """
        from .config import THRESHOLD_OVERRIDE_BOUNDS, THRESHOLD_OVERRIDE_KEYS

        if not isinstance(values, dict):
            raise ValueError("values must be an object")
        clean: dict[str, float] = {}
        for key, raw in values.items():
            if key not in THRESHOLD_OVERRIDE_KEYS:
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            low, high = THRESHOLD_OVERRIDE_BOUNDS[key]
            if not (low <= number <= high):
                raise ValueError(f"{key} must be in {low}..{high}")
            clean[key] = number
        path = self.settings.thresholds_override_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "updated_at": self._clock().isoformat(timespec="seconds"),
            "values": clean,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        LOGGER.info("threshold override written: %s values=%s", path, clean)
        return {
            "applied": clean,
            "override_path": str(path),
            "restart_required": True,
            "note": "阈值已保存，重启 posture-pr2 服务后生效。",
        }

    # ------------------------------------------------------------------
    # Missed-event marking
    # ------------------------------------------------------------------
    def add_missed(
        self,
        at: datetime,
        issue: str,
        note: str | None = None,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark a moment as a missed reminder.

        The issue class is validated against the known classes.  Metric values
        come from ``snapshot`` when the caller supplies them (the timeline
        passes the picked bucket's averages, which is more representative
        than a single sample), otherwise from the nearest stored sample.
        """
        if issue not in ISSUES:
            raise ValueError(f"invalid issue: {issue!r}")
        up = fwd = shoulder = None
        if isinstance(snapshot, dict):
            up = _coerce_float(snapshot.get("head_up"))
            fwd = _coerce_float(snapshot.get("head_fwd"))
            shoulder = _coerce_float(snapshot.get("s_norm"))
        if up is None and fwd is None:
            nearest = self._monitor.nearest(at)
            up = nearest["head_up"] if nearest else None
            fwd = nearest["head_fwd"] if nearest else None
            shoulder = nearest["s_norm"] if nearest else None
        row = self._missed.add(at, issue, note, up, fwd, shoulder)
        LOGGER.info(
            "missed event added: id=%s at=%s issue=%s snapshot=%s",
            row["id"],
            at.isoformat(timespec="seconds"),
            issue,
            up is not None or fwd is not None,
        )
        return dict(row)

    def update_missed(
        self,
        event_id: int,
        *,
        issue: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if issue is not None and issue not in ISSUES:
            raise ValueError(f"invalid issue: {issue!r}")
        row = self._missed.update(event_id, issue=issue, note=note)
        if row is None:
            raise KeyError(f"no missed event with id {event_id}")
        return dict(row)

    def delete_missed(self, event_id: int) -> bool:
        return self._missed.delete(event_id)

    def list_missed(
        self,
        *,
        hours: float = 24.0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        now = self._clock()
        since = now - timedelta(hours=max(0.1, hours))
        return [dict(row) for row in self._missed.list(since, limit)]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    LOGGER.info(
        "starting posture-reminder (schedule=%s, fps=%s, model=%s)",
        settings.schedule.summary(),
        settings.target_fps,
        settings.model_path,
    )
    service = PostureService(settings)
    service.start_api()
    try:
        service.run()
    finally:
        service.close()
    return 0
