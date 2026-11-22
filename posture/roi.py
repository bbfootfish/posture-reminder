"""Region-of-interest cropping before pose detection.

The camera sits far from the desk and covers a wide field of view.  At that
distance MediaPipe emits high-confidence false poses on desk clutter: measured
on 40 archived frames of an empty chair, 72.5% of frames produced a pose when
the crop included the desk, versus 0% when the crop was limited to the chair.
Cropping therefore is not an optimisation but the primary false-positive
control.

The ROI is expressed in normalised coordinates (0..1) so it survives a change
of capture resolution, and is configured per installation rather than guessed
in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Roi:
    """A normalised rectangle: (x1, y1) top-left, (x2, y2) bottom-right."""

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.x1 < self.x2 <= 1.0):
            raise ValueError(f"invalid x range: {self.x1}..{self.x2}")
        if not (0.0 <= self.y1 < self.y2 <= 1.0):
            raise ValueError(f"invalid y range: {self.y1}..{self.y2}")

    def as_dict(self) -> dict[str, float]:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2}

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> Roi:
        return cls(
            x1=float(values["x1"]),
            y1=float(values["y1"]),
            x2=float(values["x2"]),
            y2=float(values["y2"]),
        )


def is_identity(roi: Roi) -> bool:
    """True when the ROI covers the whole frame (cropping is a no-op)."""
    return roi.x1 <= 0.0 and roi.y1 <= 0.0 and roi.x2 >= 1.0 and roi.y2 >= 1.0


def _bounds(roi: Roi, width: int, height: int) -> tuple[int, int, int, int]:
    x1 = max(0, min(width - 1, int(round(roi.x1 * width))))
    y1 = max(0, min(height - 1, int(round(roi.y1 * height))))
    x2 = max(x1 + 1, min(width, int(round(roi.x2 * width))))
    y2 = max(y1 + 1, min(height, int(round(roi.y2 * height))))
    return x1, y1, x2, y2


def crop_with_size(frame: Any, roi: Roi) -> tuple[Any, tuple[int, int]]:
    """Crop ``frame`` to ``roi`` and report the resulting (width, height).

    The size matters: the metric extraction gates compare shoulder width
    against *frame* width, so callers must pass the cropped dimensions rather
    than the original capture dimensions.
    """
    array = np.asarray(frame)
    if array.ndim < 2 or array.size == 0:
        return frame, (0, 0)
    height, width = array.shape[:2]
    if is_identity(roi):
        return array, (width, height)
    x1, y1, x2, y2 = _bounds(roi, width, height)
    sub = array[y1:y2, x1:x2]
    if sub.size == 0:  # pragma: no cover - guarded by _bounds
        return array, (width, height)
    return sub, (sub.shape[1], sub.shape[0])


def crop(frame: Any, roi: Roi) -> Any:
    """Crop ``frame`` to ``roi``."""
    return crop_with_size(frame, roi)[0]