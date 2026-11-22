"""Integration: motion gate suppresses reminders on a static scene."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pytest

from posture.config import Settings
from posture.models import Baseline, Metrics
from posture.service import PostureService

START = datetime(2026, 11, 20, 19, 0, 0)


def _frame(seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(90, 110, size=(90, 160, 3), dtype=np.uint8)


def service_with_baseline(tmp_path, **overrides):
    from posture.baseline import BaselineStore

    BaselineStore(tmp_path / "baseline.json").save(
        Baseline(s_norm=0.087, head_up=0.38, head_fwd=164.0), samples=45
    )
    base = dict(
        rtsp_url="",
        data_dir=tmp_path,
        require_audit=False,
        motion_gate_enabled=True,
        motion_sample_interval=0.0,
    )
    base.update(overrides)
    cfg = replace(Settings(), **base)
    return PostureService(cfg)


def test_static_scene_blocks_reminder(tmp_path):
    svc = service_with_baseline(tmp_path, motion_min_moving_frames=2, motion_window_seconds=5)
    # Feed a long static scene so motion decays to inactive.
    for i in range(30):
        svc._motion.update(_frame(0), START + timedelta(seconds=i))
    assert svc._motion.active(START + timedelta(seconds=30)) is False

    # Even a strongly out-of-line posture must not fire while scene is still.
    bad = Metrics(s_norm=0.08, head_up=0.05, head_fwd=130.0)
    reminders = []
    for i in range(30, 45):
        # keep feeding static frames, as _consume_stream would in production
        svc._motion.update(_frame(0), START + timedelta(seconds=i))
        r = svc._observe(type("S", (), {"metrics": bad})(), START + timedelta(seconds=i))
        if r:
            reminders.append(r)
    assert reminders == []
    assert svc.status()["motion_active"] is False
    svc.close()


def test_moving_scene_allows_reminder(tmp_path):
    svc = service_with_baseline(tmp_path, motion_min_moving_frames=2, motion_window_seconds=10)
    # Feed a moving subject so motion is active.
    for i in range(6):
        f = _frame(0)
        x = 20 + (i % 4) * 15
        f[30:60, x : x + 20] = 220
        svc._motion.update(f, START + timedelta(seconds=i))
    assert svc._motion.active(START + timedelta(seconds=6)) is True

    bad = Metrics(s_norm=0.08, head_up=0.05, head_fwd=130.0)
    reminders = []
    for i in range(6, 40):
        # keep motion alive
        f = _frame(0)
        x = 20 + (i % 4) * 15
        f[30:60, x : x + 20] = 220
        svc._motion.update(f, START + timedelta(seconds=i))
        r = svc._observe(type("S", (), {"metrics": bad})(), START + timedelta(seconds=i))
        if r:
            reminders.append(r)
    assert len(reminders) >= 1
    svc.close()


def test_motion_gate_can_be_disabled(tmp_path):
    svc = service_with_baseline(tmp_path, motion_gate_enabled=False)
    # No motion fed at all; with gate disabled, allow must not depend on motion.
    bad = Metrics(s_norm=0.08, head_up=0.05, head_fwd=130.0)
    reminders = []
    for i in range(30):
        r = svc._observe(type("S", (), {"metrics": bad})(), START + timedelta(seconds=i))
        if r:
            reminders.append(r)
    assert len(reminders) >= 1
    assert svc.status()["motion_gate_enabled"] is False
    svc.close()
