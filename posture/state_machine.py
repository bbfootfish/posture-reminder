"""The reminder decision state machine.

This module is deliberately free of camera, MediaPipe and network imports so
the judgement rules can be unit tested anywhere.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .judge import PostureJudge, thresholds_for
from .models import ISSUES, Baseline, Metrics, Reminder
from .phrases import PHRASES
from .schedule import Mode
from .smoothing import MetricSmoother


class ReminderStateMachine:
    """Apply smoothing, the 20 s occupancy window and the speech gates.

    ``observe`` is called once per usable pose sample and returns a
    :class:`Reminder` only when every rule allows speaking.  It never talks
    to hardware; the caller owns the speaker and the database.
    """

    def __init__(
        self,
        baseline: Baseline,
        settings: Settings,
        *,
        phrases=PHRASES,
    ) -> None:
        self.settings = settings
        self.baseline = baseline
        self.phrases = phrases
        # The pose model runs on a cropped region, so s_norm is divided by the
        # crop width instead of the full frame width.  Pass both so the judge
        # can put the stored baseline on the same scale; otherwise the
        # too_close threshold sits far below every real reading.
        crop_width = int(round(settings.source_frame_width * (settings.roi_x2 - settings.roi_x1)))
        self._judge = PostureJudge(
            baseline,
            head_up_ratio=settings.head_up_ratio,
            head_fwd_drop=settings.head_fwd_drop,
            too_close_ratio=settings.too_close_ratio,
            full_frame_width=settings.source_frame_width if crop_width > 0 else 0,
            crop_width=crop_width,
        )
        self._smoother = MetricSmoother(settings.smoothing_seconds)
        # (timestamp, issue or None) for every judged sample in the window.
        self._window: deque[tuple[datetime, str | None]] = deque()
        self._last_pose_at: datetime | None = None
        self._last_reminder_at: dict[str, datetime] = {}
        # Global floor: time of the last reminder of *any* class.
        self._last_any_reminder_at: datetime | None = None
        self._reminder_history: deque[datetime] = deque()
        self._next_variant: dict[str, int] = {issue: 0 for issue in phrases}
        self._status = "starting"
        self._last_metrics: Metrics | None = None
        self._last_raw_metrics: Metrics | None = None
        self._last_issue: str | None = None
        # Whether the previous observation happened during the late-night soft
        # block.  Must be a dedicated flag: ``_status`` carries judgement
        # results (ok / out_of_line / …), so comparing it to "monitor_only"
        # would re-clear the window on every frame and the 20 s window could
        # never fill.
        self._in_monitor_only = False

    # ------------------------------------------------------------------
    # Public state (used by /healthz and tests)
    # ------------------------------------------------------------------
    @property
    def status(self) -> str:
        return self._status

    @property
    def last_metrics(self) -> Metrics | None:
        return self._last_metrics

    @property
    def window_samples(self) -> int:
        return len(self._window)

    @property
    def window_occupancy(self) -> float:
        if not self._window:
            return 0.0
        outliers = sum(1 for _, issue in self._window if issue is not None)
        return outliers / len(self._window)

    def snapshot(self, at: datetime, *, mode: Mode | None = None) -> dict[str, Any]:
        """Explain the current judgement for the dashboard.

        Everything here is read-only: the numbers are the same ones the last
        ``observe`` call used, plus live cooldown/counter values for ``at``.
        """
        span = 0.0
        if len(self._window) >= 2:
            span = (self._window[-1][0] - self._window[0][0]).total_seconds()
        cooldown_remaining = {}
        for issue in ISSUES:
            last = self._last_reminder_at.get(issue)
            remaining = 0.0
            if last is not None:
                remaining = max(0.0, self.settings.cooldown_seconds - (at - last).total_seconds())
            cooldown_remaining[issue] = round(remaining, 1)
        return {
            "status": self._status,
            "issue": self._last_issue,
            "raw": None if self._last_raw_metrics is None else self._last_raw_metrics.as_dict(),
            "smoothed": None if self._last_metrics is None else self._last_metrics.as_dict(),
            "window": {
                "samples": len(self._window),
                "span_seconds": round(span, 1),
                "seconds": self.settings.window_seconds,
                "min_span_seconds": self.settings.min_window_span_seconds,
                "occupancy": round(self.window_occupancy, 3),
                "occupancy_threshold": self.settings.occupancy_threshold,
            },
            "thresholds": thresholds_for(
                self.baseline,
                head_up_ratio=self.settings.head_up_ratio,
                head_fwd_drop=self.settings.head_fwd_drop,
                too_close_ratio=self.settings.too_close_ratio,
            ),
            "cooldown_remaining": cooldown_remaining,
            "cooldown_seconds": self.settings.cooldown_seconds,
            "hourly_count": self._count_in_current_hour(at),
            "hourly_cap": self.settings.hourly_cap,
            "hourly_unlimited": self.settings.hourly_cap <= 0,
            "last_pose_at": (
                None if self._last_pose_at is None else self._last_pose_at.isoformat(
                    timespec="seconds"
                )
            ),
            "schedule_mode": (mode or self.settings.schedule.mode(at)).value,
        }

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------
    def observe(
        self,
        metrics: Metrics | None,
        at: datetime,
        *,
        mode: Mode,
        allow_reminder: bool = True,
    ) -> Reminder | None:
        """Feed one sample (or a no-pose event) and maybe return a reminder."""
        if mode is Mode.BLOCK:
            # Nothing is measured while blocked, so any window we still hold is
            # stale.  Clear it; otherwise the first allowed sample would be
            # judged against samples from before the block.
            if self._window:
                self._window.clear()
            self._smoother.reset()
            self._last_pose_at = None
            self._last_issue = None
            self._last_raw_metrics = None
            self._last_metrics = None
            self._in_monitor_only = False
            self._status = "blocked_hours"
            return None

        entering_soft_block = mode is Mode.MONITOR_ONLY and not self._in_monitor_only
        self._in_monitor_only = mode is Mode.MONITOR_ONLY
        if entering_soft_block:
            # Drop samples judged under the old mode once, then keep judging so
            # the dashboard still shows live posture.
            self._window.clear()
            self._smoother.reset()
            self._last_pose_at = None

        if metrics is None:
            return self._observe_no_pose(at)

        if mode is Mode.MONITOR_ONLY:
            allow_reminder = False

        now = at
        self._last_pose_at = now
        self._last_raw_metrics = metrics
        smoothed = self._smoother.add(now, metrics)
        self._last_metrics = smoothed
        issue = self._judge.classify(smoothed)
        self._last_issue = issue

        self._window.append((now, issue))
        self._prune_window(now)

        if issue is None:
            self._status = "ok"
            return None

        # Do not remind on a window that has barely started.  The ticket
        # talks about the last 20 s; requiring almost the full span avoids a
        # first-frame false positive after a restart or a no-pose reset.
        span = (self._window[-1][0] - self._window[0][0]).total_seconds()
        if span < self.settings.min_window_span_seconds:
            self._status = "warming"
            return None

        if self.window_occupancy < self.settings.occupancy_threshold:
            self._status = "out_of_line"
            return None

        if not allow_reminder:
            self._status = "reminder_blocked"
            return None

        # Global floor first: it applies across classes, so alternating
        # issues cannot unlock each other.  Without this, debug mode (per-class
        # cooldown 0) produced 50 reminders in 30 seconds.
        if self._last_any_reminder_at is not None:
            since_any = (now - self._last_any_reminder_at).total_seconds()
            if since_any < self.settings.min_reminder_interval_seconds:
                self._status = "cooldown"
                return None

        last_same = self._last_reminder_at.get(issue)
        if last_same is not None:
            elapsed = (now - last_same).total_seconds()
            if elapsed <= self.settings.cooldown_seconds:
                self._status = "cooldown"
                return None

        # A cap of 0 (or negative) means no hourly limit; only the per-issue
        # cooldown and the work-hours gate apply.
        if self.settings.hourly_cap > 0 and self._count_in_current_hour(now) >= self.settings.hourly_cap:
            self._status = "hourly_cap"
            return None

        return self._emit(now, issue, smoothed)

    def _observe_no_pose(self, at: datetime) -> None:
        self._last_issue = None
        if self._last_pose_at is None:
            self._status = "no_person"
            return None
        gap = (at - self._last_pose_at).total_seconds()
        if gap >= self.settings.no_pose_reset_seconds:
            # "No one there" means the occupancy window restarts; cooldown and
            # the hourly counter are intentionally kept.
            self._window.clear()
            self._smoother.reset()
            self._last_pose_at = None
            self._last_raw_metrics = None
            self._last_metrics = None
        self._status = "no_person"
        return None

    def _emit(self, at: datetime, issue: str, metrics: Metrics) -> Reminder:
        variants = self.phrases[issue]
        variant = self._next_variant.get(issue, 0) % len(variants)
        phrase = variants[variant]
        self._next_variant[issue] = (variant + 1) % len(variants)
        self._last_reminder_at[issue] = at
        self._last_any_reminder_at = at
        self._reminder_history.append(at)
        self._prune_history(at)
        self._status = "reminded"
        return Reminder(
            at=at,
            issue=issue,
            variant=variant,
            text=phrase.text,
            phrase_file=phrase.file,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------
    def _prune_window(self, at: datetime) -> None:
        cutoff = at - timedelta(seconds=self.settings.window_seconds)
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _prune_history(self, at: datetime) -> None:
        cutoff = at - timedelta(hours=2)
        while self._reminder_history and self._reminder_history[0] < cutoff:
            self._reminder_history.popleft()

    def _count_in_current_hour(self, at: datetime) -> int:
        return sum(
            1
            for previous in self._reminder_history
            if previous.year == at.year
            and previous.month == at.month
            and previous.day == at.day
            and previous.hour == at.hour
        )
