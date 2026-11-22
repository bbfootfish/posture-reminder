"""s_norm must be measured against a stable reference.

`s_norm` is shoulder width divided by frame width.  ROI cropping changed the
*divided-by* width from 1920 px to ~767 px, inflating every reading by 2.5x
while the stored baseline kept the old scale -- so the too_close threshold
(0.0732) was satisfied by 100% of live samples and `lean` became unreachable.

Measured on 413 verified child-present samples: after rescaling the baseline,
too_close drops from 47.6% to 12.6% and lean becomes reachable (35.1%).
"""

from __future__ import annotations

import pytest

from posture.config import Settings
from posture.judge import PostureJudge, thresholds_for
from posture.models import Baseline


def _baseline(**over) -> Baseline:
    values = {"s_norm": 0.06369, "head_up": 0.363, "head_fwd": 163.7}
    values.update(over)
    return Baseline(**values)


def test_rescaled_baseline_matches_cropped_frame_scale():
    """A baseline recorded on the full frame must be rescaled for the crop."""
    j = PostureJudge(_baseline(), full_frame_width=1920, crop_width=767)
    # 0.06369 * 1920/767 = 0.1594
    assert j.too_close_threshold == pytest.approx(0.1594 * 1.15, rel=1e-3)


def test_crop_scale_one_is_a_no_op():
    """Without cropping (crop_width == full width) nothing changes."""
    j = PostureJudge(_baseline(), full_frame_width=1920, crop_width=1920)
    assert j.too_close_threshold == pytest.approx(0.06369 * 1.15, rel=1e-6)


def test_lean_class_becomes_reachable():
    """The regression: with the unscaled baseline, lean could never fire."""
    j = PostureJudge(_baseline(), full_frame_width=1920, crop_width=767)
    # Forward collapse, slender shoulders -> lean (not too_close).
    assert j.classify(_metrics(s_norm=0.12, head_up=0.40, head_fwd=135.0)) == "lean"
    # Forward collapse with wide shoulders -> close to the desk.
    assert j.classify(_metrics(s_norm=0.22, head_up=0.40, head_fwd=135.0)) == "too_close"


def test_head_down_takes_priority_over_distance():
    j = PostureJudge(_baseline(), full_frame_width=1920, crop_width=767)
    assert j.classify(_metrics(s_norm=0.25, head_up=0.10, head_fwd=130.0)) == "head_down"


def test_thresholds_for_reports_the_scaled_value():
    """The dashboard must show the same number the judge uses."""
    got = thresholds_for(_baseline(), full_frame_width=1920, crop_width=767)
    assert got["too_close_s_norm"] == pytest.approx(0.1594 * 1.15, rel=1e-3)


def test_bad_crop_width_is_rejected():
    with pytest.raises(ValueError):
        PostureJudge(_baseline(), full_frame_width=1920, crop_width=0)


def _metrics(**kw):
    from posture.models import Metrics

    return Metrics(**kw)
