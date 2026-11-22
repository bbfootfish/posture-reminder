"""Frame-difference presence gate (no camera, pure numpy)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from posture.motion import MotionDetector

START = datetime(2026, 11, 20, 19, 0, 0)
SIZE = (90, 160, 3)  # small synthetic BGR frames


def _frame(seed: int = 0, blob: bool = False) -> np.ndarray:
    """Deterministic frame; blob adds moving foreground to one corner."""
    rng = np.random.default_rng(seed)
    frame = rng.integers(90, 110, size=SIZE, dtype=np.uint8)
    if blob:
        frame[20:50, 30:70] = 220  # a bright moving subject
    return frame


def test_static_scene_is_not_active():
    """An empty chair: identical frames must never report motion."""
    det = MotionDetector(sample_interval=0.0)
    at = START
    for i in range(30):
        det.update(_frame(0), at + timedelta(seconds=i))
    assert det.active(at + timedelta(seconds=30)) is False


def test_tiny_compression_noise_is_not_motion():
    """Small global jitter (compression/light drift) must be ignored."""
    det = MotionDetector(sample_interval=0.0)
    base = _frame(0).astype(np.int16)
    at = START
    for i in range(30):
        noise = np.random.default_rng(i).integers(-2, 3, size=SIZE, dtype=np.int16)
        frame = np.clip(base + noise, 0, 255).astype(np.uint8)
        det.update(frame, at + timedelta(seconds=i))
    assert det.active(at + timedelta(seconds=30)) is False


def test_moving_subject_is_active():
    """A foreground blob moving across the frame means someone is there."""
    det = MotionDetector(sample_interval=0.0)
    at = START
    for i in range(20):
        frame = _frame(0)
        x = 20 + (i % 5) * 8  # subject shifts horizontally each frame
        frame[30:60, x : x + 20] = 220
        det.update(frame, at + timedelta(seconds=i))
    assert det.active(at + timedelta(seconds=20)) is True


def test_motion_expires_after_window():
    """Motion must age out so a person who leaves stops gating forever."""
    det = MotionDetector(sample_interval=0.0, window_seconds=5.0, min_moving_frames=2)
    at = START
    for i in range(4):
        frame = _frame(0)
        x = 20 + (i % 5) * 8
        frame[30:60, x : x + 20] = 220
        det.update(frame, at + timedelta(seconds=i))
    assert det.active(at + timedelta(seconds=4)) is True
    # Now feed static frames longer than the window.
    for i in range(4, 20):
        det.update(_frame(0), at + timedelta(seconds=i))
    assert det.active(at + timedelta(seconds=20)) is False


def test_sample_interval_throttles_updates():
    """Frames closer than the interval are ignored (cheap + de-noised)."""
    det = MotionDetector(sample_interval=1.0, window_seconds=5.0, min_moving_frames=2)
    at = START
    moved = 0
    for i in range(20):
        frame = _frame(0)
        if i % 2 == 0:
            frame[30:60, 20:40] = 220
        before = det.samples_count
        det.update(frame, at + timedelta(milliseconds=500 * i))
        if det.samples_count > before:
            moved += 1
    assert moved <= 11  # roughly half the frames, throttled
