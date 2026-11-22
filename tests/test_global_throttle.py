"""A global floor between reminders of *any* class.

Debug mode sets the per-class cooldown to zero so thresholds can be observed
quickly.  Per-class cooldown cannot throttle alternating classes: `too_close`
and `head_down` unlock each other, which produced 50 spoken reminders in 30
seconds on 2026-11-21 while a child was sitting at the desk.  A global interval
is the backstop.
"""

from __future__ import annotations

from datetime import timedelta

from posture.config import Settings
from posture.models import Metrics

from .helpers import HEAD_DOWN, LEAN, START, engine, feed


def test_global_interval_default_is_thirty_seconds():
    assert Settings().min_reminder_interval_seconds == 30.0


def test_debug_mode_still_has_a_global_floor():
    """Even with the per-class cooldown zeroed, reminders must be spaced."""
    machine = engine(smoothing_seconds=0.0, cooldown_seconds=0.0, min_reminder_interval_seconds=30.0)
    # 60 s of constantly out-of-line posture would previously fire a reminder on
    # every judged sample (5 fps -> ~300 reminders).
    reminders, _ = feed(machine, START, 60.0, HEAD_DOWN)
    assert len(reminders) >= 1
    assert len(reminders) <= 3, f"expected ~2 reminders in 60 s, got {len(reminders)}"
    for earlier, later in zip(reminders, reminders[1:]):
        gap = (later.at - earlier.at).total_seconds()
        assert gap >= 30.0, f"reminders {gap:.1f}s apart, floor is 30s"


def test_global_floor_applies_across_different_classes():
    """Alternating classes are exactly the failure this guards against."""
    machine = engine(smoothing_seconds=0.0, cooldown_seconds=0.0, min_reminder_interval_seconds=30.0)
    issues = []

    def alternating(second: float) -> Metrics:
        return HEAD_DOWN if int(second) % 2 == 0 else LEAN

    reminders, _ = feed(machine, START, 60.0, alternating)
    issues = [r.issue for r in reminders]
    assert len(reminders) <= 3, f"alternating classes defeated the floor: {issues}"
    for earlier, later in zip(reminders, reminders[1:]):
        assert (later.at - earlier.at).total_seconds() >= 30.0


def test_normal_cooldown_is_not_lengthened():
    """With the production 300 s cooldown the 30 s floor never binds."""
    machine = engine(smoothing_seconds=0.0, cooldown_seconds=300.0)
    reminders, _ = feed(machine, START, 60.0, HEAD_DOWN)
    assert len(reminders) == 1
