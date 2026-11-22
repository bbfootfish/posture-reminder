"""Shared, dependency-free data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Reminder classes.  Keep these values stable: they are used in phrase
# filenames, SQLite rows and the SPEAK_CMD template.
TOO_CLOSE = "too_close"
HEAD_DOWN = "head_down"
LEAN = "lean"
ISSUES: tuple[str, ...] = (TOO_CLOSE, HEAD_DOWN, LEAN)


@dataclass(frozen=True)
class Metrics:
    """The three side-view metrics defined in the ticket."""

    s_norm: float
    head_up: float
    head_fwd: float

    def as_dict(self) -> dict[str, float]:
        return {
            "s_norm": round(self.s_norm, 6),
            "head_up": round(self.head_up, 6),
            "head_fwd": round(self.head_fwd, 3),
        }


@dataclass(frozen=True)
class Baseline:
    """Calibrated metrics recorded while the child is writing normally."""

    s_norm: float
    head_up: float
    head_fwd: float

    def as_dict(self) -> dict[str, float]:
        return {
            "s_norm": round(self.s_norm, 6),
            "head_up": round(self.head_up, 6),
            "head_fwd": round(self.head_fwd, 3),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Baseline:
        return cls(
            s_norm=float(raw["s_norm"]),
            head_up=float(raw["head_up"]),
            head_fwd=float(raw["head_fwd"]),
        )

    def is_usable(self) -> bool:
        return self.s_norm > 0 and self.head_up > 0


@dataclass(frozen=True)
class Phrase:
    """One fixed, pre-generated spoken line."""

    text: str
    file: str


@dataclass(frozen=True)
class Reminder:
    """A reminder that passed every gate and was handed to the speaker."""

    at: datetime
    issue: str
    variant: int
    text: str
    phrase_file: str
    metrics: Metrics
    image: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(timespec="seconds"),
            "issue": self.issue,
            "variant": self.variant,
            "text": self.text,
            "phrase_file": self.phrase_file,
            "image": self.image,
        }
