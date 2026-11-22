"""Frame-difference presence gate.

The pose model produces high-confidence false detections on empty chairs and
clutter.  Metric-range gates reject most of them, but some false poses land in
the plausible human range.  A reliable independent signal is motion: a seated
child breathes, writes and shifts, while an empty chair is essentially static.

This module is deliberately camera-agnostic and testable with numpy arrays:
it downsamples each BGR frame to a small grayscale image, compares it to the
previous sampled frame, and records whether a meaningful fraction of pixels
changed.  ``active`` is true when enough motion was seen recently.  Until a
short baseline of samples exists it returns True (fail-open), so a fresh start
never silences reminders merely because motion has not been measured yet.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Any

import numpy as np

_SMALL = (160, 90)  # width, height for the grayscale difference image


class MotionDetector:
    """Rolling frame-difference motion gate.

    Args:
        sample_interval: minimum seconds between processed frames.
        window_seconds: how long a motion observation remains valid.
        min_moving_frames: number of recent moving samples required to be
            considered "someone is present".
        change_threshold: grayscale delta above which a pixel counts as
            changed.  Must exceed HEVC compression jitter (~1-2 levels).
        min_changed_ratio: fraction of pixels that must change in a sample
            for it to count as motion.  Empty scenes measured 0.1-1%.
    """

    def __init__(
        self,
        *,
        sample_interval: float = 0.5,
        window_seconds: float = 15.0,
        min_moving_frames: int = 2,
        change_threshold: int = 18,
        min_changed_ratio: float = 0.03,
    ) -> None:
        if sample_interval < 0 or window_seconds <= 0 or min_moving_frames < 1:
            raise ValueError("invalid motion detector configuration")
        self.sample_interval = float(sample_interval)
        self.window_seconds = float(window_seconds)
        self.min_moving_frames = int(min_moving_frames)
        self.change_threshold = int(change_threshold)
        self.min_changed_ratio = float(min_changed_ratio)
        # (timestamp seconds, moving bool)
        self._samples: deque[tuple[float, bool]] = deque()
        self._previous: np.ndarray | None = None
        self._last_sample_at: float | None = None

    @property
    def samples_count(self) -> int:
        return len(self._samples)

    @staticmethod
    def _gray(frame: Any) -> np.ndarray:
        array = np.asarray(frame)
        if array.size == 0:
            return np.zeros((_SMALL[1], _SMALL[0]), dtype=np.int16)
        gray = _to_gray(array)
        small = _resize(gray)
        return small.astype(np.int16)

    def update(self, frame: Any, at: datetime) -> bool | None:
        """Feed one frame.  Returns whether this sample moved, or None if
        the frame was throttled / used only as the first baseline."""
        now = at.timestamp()
        if self._last_sample_at is not None and (now - self._last_sample_at) < self.sample_interval:
            return None
        self._last_sample_at = now

        current = self._gray(frame)
        moving: bool | None = None
        if self._previous is not None:
            delta = np.abs(current - self._previous)
            changed_ratio = float(np.count_nonzero(delta > self.change_threshold)) / delta.size
            moving = changed_ratio >= self.min_changed_ratio
            self._samples.append((now, bool(moving)))
        self._previous = current
        self._prune(now)
        return moving

    def active(self, at: datetime) -> bool:
        """True when enough motion was observed in the recent window.

        Fail-open while fewer samples exist than needed, so startup does not
        suppress reminders before motion can be measured.
        """
        self._prune(at.timestamp())
        if len(self._samples) < self.min_moving_frames:
            return True
        return sum(1 for _, moving in self._samples if moving) >= self.min_moving_frames

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()


def _to_gray(array: np.ndarray) -> np.ndarray:
    """Grayscale without requiring OpenCV: BT.601 luma on H,W,3 arrays."""
    if array.ndim == 2:
        return array
    if array.ndim == 3 and array.shape[2] >= 3:
        b = array[..., 0].astype(np.float32)
        g = array[..., 1].astype(np.float32)
        r = array[..., 2].astype(np.float32)
        return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.uint8)
    return np.zeros(array.shape[:2], dtype=np.uint8)


def _resize(gray: np.ndarray) -> np.ndarray:
    """Nearest-neighbour downsample to _SMALL, numpy only."""
    h, w = gray.shape[:2]
    if h == 0 or w == 0:
        return np.zeros((_SMALL[1], _SMALL[0]), dtype=np.uint8)
    ys = (np.linspace(0, h - 1, _SMALL[1]).astype(int))
    xs = (np.linspace(0, w - 1, _SMALL[0]).astype(int))
    return gray[ys[:, None], xs[None, :]]
