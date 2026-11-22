"""Shared test constants and small feeders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from posture.config import Settings
from posture.models import Baseline, Metrics
from posture.schedule import Mode
from posture.state_machine import ReminderStateMachine

BASE = Baseline(s_norm=0.0875, head_up=0.38, head_fwd=164.0)
START = datetime(2026, 11, 13, 16, 0, 0)

NORMAL = Metrics(s_norm=0.0875, head_up=0.38, head_fwd=164.0)
HEAD_DOWN = Metrics(s_norm=0.0875, head_up=0.12, head_fwd=155.0)
LEAN = Metrics(s_norm=0.0875, head_up=0.38, head_fwd=130.0)


def settings(**overrides) -> Settings:
    return replace(Settings(), **overrides)


def engine(**overrides) -> ReminderStateMachine:
    return ReminderStateMachine(BASE, settings(**overrides))


def feed(
    machine: ReminderStateMachine,
    start: datetime,
    seconds: float,
    value: Metrics | None | Callable[[float], Metrics | None],
    *,
    fps: float = 5.0,
    mode: Mode | None = None,
):
    """Feed ``seconds`` of samples at ``fps``; returns (reminders, last_time).

    ``mode`` defaults to ``Mode.ALLOW`` so the existing judgement tests keep
    exercising the rules regardless of what weekday/time ``START`` happens to
    be.  ``START`` is 2026-11-13 16:00 — a Friday inside school hours — so
    deriving the mode from the schedule here would put every one of them in
    ``BLOCK`` and silently void the whole suite.  Tests that care about the
    schedule pass ``mode=`` explicitly.
    """
    resolved_mode = Mode.ALLOW if mode is None else mode
    reminders = []
    count = int(round(seconds * fps))
    last = start
    for index in range(count + 1):
        at = start + timedelta(seconds=index / fps)
        sample = value(index / fps) if callable(value) else value
        reminder = machine.observe(sample, at, mode=resolved_mode)
        if reminder is not None:
            reminders.append(reminder)
        last = at
    return reminders, last
