"""head_up gate must accept a child leaning over the desk.

Measured on 119 verified child-present frames from the installed camera: the
nose sits below the shoulder line in 74% of frames (median head_up -0.142,
p5 -0.389) simply because the child is drawing while leaning forward.  The
original +0.03 floor rejected 75% of real posture.
"""

from __future__ import annotations

import pytest

from posture import metrics as M


class _P:
    __slots__ = ("x", "y", "z", "visibility", "presence")

    def __init__(self, x: float, y: float, visibility: float = 1.0) -> None:
        self.x = x
        self.y = y
        self.z = 0.0
        self.visibility = visibility
        self.presence = 1.0


def _pose(*, nose_below_shoulders: float, shoulder_w: float = 0.12) -> list[_P]:
    """33 landmarks; nose offset below the shoulder midpoint in shoulder widths."""
    pts = [_P(0.5, 0.5) for _ in range(33)]
    cx, cy, half = 0.5, 0.5, shoulder_w / 2.0
    pts[11] = _P(cx - half, cy)          # left shoulder
    pts[12] = _P(cx + half, cy)          # right shoulder
    pts[0] = _P(cx, cy + nose_below_shoulders * shoulder_w)   # nose
    pts[23] = _P(cx - half, cy + 0.25)   # left hip
    pts[24] = _P(cx + half, cy + 0.25)   # right hip
    pts[7] = _P(cx - half, cy - 0.05)    # left ear
    pts[8] = _P(cx + half, cy - 0.05)    # right ear
    return pts


def test_gate_constant_admits_real_forward_lean():
    # p5 of real data is -0.389; the gate must sit below that.
    assert M.MIN_HEAD_UP <= -0.45


def test_extract_sample_accepts_forward_lean():
    sample = M.extract_sample(_pose(nose_below_shoulders=0.20), 400, 600)
    assert sample is not None
    assert sample.metrics.head_up < 0


def test_extract_sample_accepts_upright_posture():
    sample = M.extract_sample(_pose(nose_below_shoulders=-0.5), 400, 600)
    assert sample is not None
    assert sample.metrics.head_up > 0


def test_extract_sample_rejects_impossible_skeleton():
    # Nose three shoulder-widths below the shoulders is not a person.
    assert M.extract_sample(_pose(nose_below_shoulders=3.0), 400, 600) is None
