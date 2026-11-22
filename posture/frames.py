"""Frame quality gates.

HEVC streams emit two or three seconds of near-uniform grey frames right
after connecting.  The pose model happily produces high-confidence garbage on
them, so they must never reach the state machine.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def gray_std(frame: Any) -> float:
    """Return the standard deviation of the frame's luminance.

    The calculation is done with NumPy only so the quality gate can be tested
    without OpenCV; the camera path passes a BGR ``ndarray`` here.
    """
    if frame is None:
        return 0.0
    array = np.asarray(frame)
    if array.size == 0:
        return 0.0
    if array.ndim == 2:
        luminance = array.astype(np.float32)
    elif array.ndim == 3 and array.shape[2] >= 3:
        # OpenCV order is BGR; ITU-R BT.601 luma coefficients.
        bgr = array[..., :3].astype(np.float32)
        luminance = 0.114 * bgr[..., 0] + 0.587 * bgr[..., 1] + 0.299 * bgr[..., 2]
    else:
        return 0.0
    return float(luminance.std())


def frame_is_usable(frame: Any, min_std: float = 10.0) -> bool:
    """False for the grey start-up frames (and empty frames)."""
    return gray_std(frame) >= min_std
