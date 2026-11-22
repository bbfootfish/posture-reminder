"""ROI cropping: only the chair region feeds the pose model.

Every non-empty historical sample was a false pose produced on an empty
chair. Testing showed the false poses came from desk clutter (books, a pink
bag, a lamp) rather than the chair itself: cropping to the chair dropped the
false-positive rate from 72.5% to 0% over 40 archived frames.
"""

from __future__ import annotations

import numpy as np

from posture.roi import Roi, crop, is_identity


def test_identity_roi_keeps_frame_unchanged():
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    roi = Roi(x1=0.0, y1=0.0, x2=1.0, y2=1.0)
    assert is_identity(roi) is True
    out = crop(frame, roi)
    assert out.shape == frame.shape


def test_crop_extracts_expected_region():
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    frame[20:60, 100:160] = 255  # bright block
    roi = Roi(x1=0.5, y1=0.2, x2=0.8, y2=0.6)
    out = crop(frame, roi)
    assert out.shape[0] == 40 and out.shape[1] == 60
    assert out.mean() > 250  # the block fills the crop


def test_roi_validation_rejects_out_of_range_box():
    """A mis-configured ROI must fail loudly at startup, not silently clamp:
    clamping would hide a typo (e.g. 0.8 written as 8) behind a plausible
    crop."""
    import pytest

    with pytest.raises(ValueError):
        Roi(x1=0.6, y1=0.2, x2=0.4, y2=0.8)  # x1 > x2
    with pytest.raises(ValueError):
        Roi(x1=0.2, y1=0.5, x2=0.8, y2=0.5)  # zero height
    with pytest.raises(ValueError):
        Roi(x1=-1.0, y1=-1.0, x2=5.0, y2=5.0)  # outside the frame
    with pytest.raises(ValueError):
        Roi(x1=0.2, y1=0.2, x2=0.8, y2=1.5)  # y2 beyond 1.0


def test_shoulder_pct_uses_cropped_dimensions():
    """After cropping, frame width must be the crop width or the shoulder
    percentage gate would reject every real subject."""
    from posture.roi import crop_with_size

    frame = np.zeros((1000, 1000, 3), dtype=np.uint8)
    roi = Roi(x1=0.5, y1=0.0, x2=0.8, y2=1.0)  # 300 x 1000 crop
    sub, (w, h) = crop_with_size(frame, roi)
    assert (w, h) == (sub.shape[1], sub.shape[0])
    assert w == 300
