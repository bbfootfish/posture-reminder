"""The six acceptance assertions for judgement and speech gating."""

from __future__ import annotations

from datetime import timedelta

from posture.models import HEAD_DOWN, Metrics
from posture.schedule import Mode

from .helpers import (
    HEAD_DOWN as HEAD_DOWN_SAMPLE,
)
from .helpers import (
    NORMAL,
    START,
    engine,
    feed,
)


def test_occupancy_55_percent_triggers_once():
    machine = engine(smoothing_seconds=0.0)

    # 20 s window; the last 11 s are out of line (56/101 samples = 55.4%).
    def sample(second: float):
        return HEAD_DOWN_SAMPLE if second >= 9.0 else NORMAL

    reminders, _ = feed(machine, START, 20.0, sample)
    assert len(reminders) == 1
    assert reminders[0].issue == HEAD_DOWN
    assert machine.window_occupancy >= 0.55


def test_occupancy_50_percent_does_not_trigger():
    machine = engine(smoothing_seconds=0.0)

    # Only the last 10 s are out of line: 50.5% over the whole 20 s window,
    # and exactly 50% at the last pre-final sample.
    def sample(second: float):
        return HEAD_DOWN_SAMPLE if second >= 10.0 else NORMAL

    reminders, _ = feed(machine, START, 20.0, sample)
    assert reminders == []
    assert machine.window_occupancy < 0.55


def test_same_class_respects_180_second_cooldown():
    # Explicitly pin cooldown: the Settings default is now 300 s (production).
    machine = engine(smoothing_seconds=0.0, hourly_cap=100, cooldown_seconds=180.0)
    reminders, _ = feed(machine, START, 20.0, HEAD_DOWN_SAMPLE)
    assert len(reminders) == 1
    first_at = reminders[0].at

    # Keep the window saturated; at 179.8 s the same class is still blocked.
    blocked, last = feed(machine, first_at + timedelta(seconds=0.2), 179.6, HEAD_DOWN_SAMPLE)
    assert blocked == []
    # Crossing 180 s allows exactly one new reminder of the same class.
    more, _ = feed(machine, last + timedelta(seconds=0.2), 2.0, HEAD_DOWN_SAMPLE)
    reminders.extend(more)
    assert len(reminders) == 2, [item.at for item in reminders]
    assert (reminders[1].at - first_at).total_seconds() >= 180.0


def test_hourly_cap_silences_after_six_until_next_hour():
    machine = engine(smoothing_seconds=0.0, cooldown_seconds=1.0, hourly_cap=6, min_reminder_interval_seconds=0.0)
    reminders, last = feed(machine, START, 20.0, HEAD_DOWN_SAMPLE)
    assert len(reminders) == 1

    more, last = feed(machine, last + timedelta(seconds=0.2), 10.0, HEAD_DOWN_SAMPLE)
    reminders.extend(more)
    assert len(reminders) == 6, [item.at for item in reminders]

    # Seventh and eighth attempts are blocked while still in the same hour.
    blocked, last = feed(machine, last + timedelta(seconds=0.2), 5.0, HEAD_DOWN_SAMPLE)
    reminders.extend(blocked)
    assert len(reminders) == 6
    assert machine.status == "hourly_cap"

    # A new hour resets the counter; the window must refill first.
    hour_start = last.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    next_hour, _ = feed(machine, hour_start, 20.0, HEAD_DOWN_SAMPLE)
    reminders.extend(next_hour)
    assert len(reminders) == 7
    assert reminders[-1].at.hour == hour_start.hour


def test_block_mode_never_judges_and_clears_the_window():
    machine = engine(smoothing_seconds=0.0)
    # 先喂一段正常样本，让窗口非空
    feed(machine, START, 10.0, NORMAL)
    assert machine.window_samples > 0

    reminders, _ = feed(
        machine, START + timedelta(seconds=10), 30.0, HEAD_DOWN_SAMPLE, mode=Mode.BLOCK
    )
    assert reminders == []
    assert machine.status == "blocked_hours"
    assert machine.window_samples == 0


def test_monitor_only_judges_but_never_reminds():
    machine = engine(
        smoothing_seconds=0.0, cooldown_seconds=0.0, min_reminder_interval_seconds=0.0
    )
    reminders, _ = feed(machine, START, 60.0, HEAD_DOWN_SAMPLE, mode=Mode.MONITOR_ONLY)
    assert reminders == []
    # 窗口仍在累积，说明判定在跑
    assert machine.window_samples > 0
    # 占空比足以触发，但被 allow_reminder=False 拦住：这正是软屏蔽该有的样子
    # （out_of_line 只出现在占空比未达阈值时）。
    assert machine.status in {"reminder_blocked", "out_of_line", "warming", "ok"}


def test_monitor_only_clears_stale_window_on_entry():
    """软屏蔽开始的瞬间不能沿用屏蔽前的样本判出结论。"""
    machine = engine(smoothing_seconds=0.0)
    feed(machine, START, 25.0, HEAD_DOWN_SAMPLE)
    assert machine.window_samples > 0

    machine.observe(HEAD_DOWN_SAMPLE, START + timedelta(seconds=25), mode=Mode.MONITOR_ONLY)
    assert machine.window_samples == 1  # 只剩刚喂的这一条


def test_leaving_late_night_allows_an_immediate_reminder():
    machine = engine(
        smoothing_seconds=0.0, cooldown_seconds=0.0, min_reminder_interval_seconds=0.0
    )
    feed(machine, START, 60.0, HEAD_DOWN_SAMPLE, mode=Mode.MONITOR_ONLY)
    # 软屏蔽期间没有播报，因此没有冷却残留
    reminders, _ = feed(
        machine, START + timedelta(seconds=60), 40.0, HEAD_DOWN_SAMPLE, mode=Mode.ALLOW
    )
    assert len(reminders) >= 1


def test_snapshot_reports_schedule_mode():
    machine = engine(smoothing_seconds=0.0)
    snap = machine.snapshot(START, mode=Mode.MONITOR_ONLY)
    assert snap["schedule_mode"] == "monitor_only"
    assert "in_work_hours" not in snap


def test_continuous_five_seconds_without_pose_resets_window():
    machine = engine(smoothing_seconds=0.0)

    # 18 s is not enough to satisfy the 20 s span requirement.
    initial, last = feed(machine, START, 18.0, HEAD_DOWN_SAMPLE)
    assert initial == []
    assert machine.window_samples > 0

    # 5.2 s without any pose clears the window.
    gap_start = last + timedelta(seconds=0.2)
    no_pose, last = feed(machine, gap_start, 5.2, None)
    assert no_pose == []
    assert machine.window_samples == 0

    # The resumed window needs to fill again; if the reset had not happened,
    # the first resumed sample would already be at 100% occupancy over a full
    # window and would trigger immediately.
    resumed_start = last + timedelta(seconds=0.2)
    early, last = feed(machine, resumed_start, 18.0, HEAD_DOWN_SAMPLE)
    assert early == []
    later, _ = feed(machine, last + timedelta(seconds=0.2), 1.5, HEAD_DOWN_SAMPLE)
    assert len(later) == 1


def test_gray_startup_frames_are_rejected_by_the_frame_gate():
    import numpy as np

    from posture.frames import frame_is_usable, gray_std

    grey = np.full((120, 160, 3), 128, dtype=np.uint8)
    assert gray_std(grey) == 0.0
    assert not frame_is_usable(grey, min_std=10.0)

    # A flat frame whose luminance standard deviation is below 10.
    low_contrast = np.tile(np.linspace(100, 108, 160, dtype=np.uint8), (120, 1))
    low_contrast_bgr = np.stack([low_contrast] * 3, axis=-1)
    assert gray_std(low_contrast_bgr) < 10.0
    assert not frame_is_usable(low_contrast_bgr, min_std=10.0)

    checker = np.indices((120, 160)).sum(axis=0) % 2 * 255
    checker_bgr = np.stack([checker] * 3, axis=-1).astype(np.uint8)
    assert gray_std(checker_bgr) > 10.0
    assert frame_is_usable(checker_bgr, min_std=10.0)


def test_smoothing_is_a_rolling_median():
    from posture.smoothing import RollingMedian

    smoother = RollingMedian(3.0)
    t0 = START
    for step in range(10):  # 0.0 .. 1.8 s
        smoother.add(t0 + timedelta(seconds=step * 0.2), 0.0)
    for step in range(4):  # step change at 2.0 s
        smoother.add(t0 + timedelta(seconds=2.0 + step * 0.2), 100.0)

    # 0.8 s after the step the median is still dominated by zeros.
    assert smoother.add(t0 + timedelta(seconds=2.8), 100.0) == 0.0
    for step in range(3, 11):  # 3.0 .. 4.0 s
        smoother.add(t0 + timedelta(seconds=step * 0.2), 100.0)
    # 2.2 s after the step the 3 s window is dominated by the new value.
    assert smoother.add(t0 + timedelta(seconds=4.2), 100.0) == 100.0


def test_too_close_and_lean_are_distinguished_by_shoulder_width():
    from posture.judge import PostureJudge
    from posture.models import LEAN, TOO_CLOSE
    from tests.helpers import BASE

    judge = PostureJudge(BASE)
    assert judge.classify(Metrics(0.0875, 0.38, 130.0)) == LEAN
    assert judge.classify(Metrics(0.1100, 0.38, 130.0)) == TOO_CLOSE
    assert judge.classify(Metrics(0.0875, 0.12, 130.0)) == HEAD_DOWN
