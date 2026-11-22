"""Rolling median smoothing.

The ticket compares postures only after a 3 second rolling median.  The
median is deliberately used instead of a mean: a brief head movement must
not dominate the judgement.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from statistics import median

from .models import Metrics


class RollingMedian:
    """Median of the samples inside a trailing time window."""

    def __init__(self, window_seconds: float) -> None:
        if window_seconds < 0:
            raise ValueError("window_seconds must be >= 0")
        self.window_seconds = window_seconds
        self._samples: deque[tuple[datetime, float]] = deque()

    def add(self, at: datetime, value: float) -> float:
        self._samples.append((at, float(value)))
        self._prune(at)
        return median(value for _, value in self._samples)

    def _prune(self, at: datetime) -> None:
        if self.window_seconds <= 0:
            while len(self._samples) > 1:
                self._samples.popleft()
            return
        cutoff = at.timestamp() - self.window_seconds
        while self._samples and self._samples[0][0].timestamp() < cutoff:
            self._samples.popleft()

    def reset(self) -> None:
        self._samples.clear()

    def __len__(self) -> int:
        return len(self._samples)


class MetricSmoother:
    """Three independent rolling medians, one per metric."""

    def __init__(self, window_seconds: float) -> None:
        self.s_norm = RollingMedian(window_seconds)
        self.head_up = RollingMedian(window_seconds)
        self.head_fwd = RollingMedian(window_seconds)

    def add(self, at: datetime, metrics: Metrics) -> Metrics:
        return Metrics(
            s_norm=self.s_norm.add(at, metrics.s_norm),
            head_up=self.head_up.add(at, metrics.head_up),
            head_fwd=self.head_fwd.add(at, metrics.head_fwd),
        )

    def reset(self) -> None:
        self.s_norm.reset()
        self.head_up.reset()
        self.head_fwd.reset()
