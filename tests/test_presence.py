"""Presence timer used by the automatic camera audit."""

from __future__ import annotations

from datetime import timedelta

from posture.presence import PresenceTimer

from .helpers import START


def test_presence_must_last_the_configured_duration():
    timer = PresenceTimer(10.0)
    assert timer.update(True, START) is False
    assert timer.update(True, START + timedelta(seconds=9.8)) is False
    assert timer.update(True, START + timedelta(seconds=10.0)) is True
    # Only one trigger until reset.
    assert timer.update(True, START + timedelta(seconds=12.0)) is False


def test_short_gap_is_tolerated_but_long_gap_resets():
    timer = PresenceTimer(5.0, grace_seconds=1.0)
    assert timer.update(True, START) is False
    assert timer.update(False, START + timedelta(seconds=0.5)) is False
    assert timer.update(True, START + timedelta(seconds=1.0)) is False
    assert timer.update(True, START + timedelta(seconds=5.2)) is True

    assert timer.update(False, START + timedelta(seconds=6.0)) is False
    assert timer.update(False, START + timedelta(seconds=8.0)) is False  # reset
    assert timer.update(True, START + timedelta(seconds=9.0)) is False
    assert timer.update(True, START + timedelta(seconds=13.9)) is False
    assert timer.update(True, START + timedelta(seconds=14.0)) is True


def test_reset_rearms_the_timer():
    timer = PresenceTimer(2.0)
    assert timer.update(True, START) is False
    assert timer.update(True, START + timedelta(seconds=3.0)) is True
    timer.reset()
    assert timer.update(True, START + timedelta(seconds=4.0)) is False
    assert timer.update(True, START + timedelta(seconds=6.0)) is True
