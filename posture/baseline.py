"""Baseline loading, saving and the 2 second calibration session."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median

from .models import Baseline, Metrics


class BaselineError(RuntimeError):
    """A baseline could not be read, written or produced."""


@dataclass(frozen=True)
class CalibrationResult:
    baseline: Baseline
    samples: int
    started_at: datetime
    finished_at: datetime


class BaselineStore:
    """Reads and atomically writes ``baseline.json``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Baseline | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            baseline = Baseline.from_dict(raw.get("metrics", raw))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BaselineError(f"cannot read {self.path}: {exc}") from exc
        if not baseline.is_usable():
            raise BaselineError(f"baseline in {self.path} is not usable")
        return baseline

    def save(self, baseline: Baseline, *, samples: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": "calibrate",
            "samples": samples,
            "metrics": baseline.as_dict(),
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise BaselineError(f"cannot write {self.path}: {exc}") from exc


class CalibrationSession:
    """Collect valid samples for ``duration_seconds`` and take the median."""

    def __init__(
        self,
        duration_seconds: float = 2.0,
        min_samples: int = 5,
    ) -> None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        self.duration_seconds = duration_seconds
        self.min_samples = max(2, int(min_samples))
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self._samples: list[Metrics] = []

    def start(self, at: datetime) -> None:
        if self.started_at is None:
            self.started_at = at

    def add(self, metrics: Metrics | None, at: datetime) -> bool:
        """Feed one frame; returns True once the collection duration is over."""
        self.start(at)
        if metrics is not None:
            self._samples.append(metrics)
        if self.finished_at is None:
            elapsed = (at - self.started_at).total_seconds()  # type: ignore[operator]
            if elapsed >= self.duration_seconds:
                self.finished_at = at
        return self.finished_at is not None

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    def result(self) -> CalibrationResult:
        if self.started_at is None or self.finished_at is None:
            raise BaselineError("calibration has not finished")
        if len(self._samples) < self.min_samples:
            raise BaselineError(
                f"only {len(self._samples)} valid pose samples during calibration; "
                f"need at least {self.min_samples}"
            )
        baseline = Baseline(
            s_norm=float(median(sample.s_norm for sample in self._samples)),
            head_up=float(median(sample.head_up for sample in self._samples)),
            head_fwd=float(median(sample.head_fwd for sample in self._samples)),
        )
        return CalibrationResult(
            baseline=baseline,
            samples=len(self._samples),
            started_at=self.started_at,
            finished_at=self.finished_at,
        )
