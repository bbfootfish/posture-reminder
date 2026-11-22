"""Threshold suggestion logic is conservative and review-gated."""

from __future__ import annotations

import pytest

from posture.config import THRESHOLD_OVERRIDE_BOUNDS, Settings
from posture.models import Baseline
from posture.tuning import suggest


BASE = Baseline(s_norm=0.0875, head_up=0.38, head_fwd=164.0)
POLICY = {
    "head_up_ratio": 0.5,
    "head_fwd_drop": 15.0,
    "too_close_ratio": 1.15,
    "occupancy_threshold": 0.55,
}


def _bucket(issue, verdict, count, up_avg, up_max, fwd_avg, fwd_min):
    return {
        "issue": issue,
        "verdict": verdict,
        "count": count,
        "head_up": {"avg": up_avg, "min": up_avg, "max": up_max},
        "head_fwd": {"avg": fwd_avg, "min": fwd_min, "max": fwd_avg},
        "s_norm": {"avg": 0.09, "min": 0.08, "max": 0.1},
    }


def test_too_few_labels_yield_no_changes():
    stats = [_bucket("head_down", "false_positive", 3, 0.3, 0.35, 160, 150)]
    result = suggest(stats, BASE, POLICY)
    assert result["changes"] == {}
    assert result["confidence"] == "low"
    assert result["suggested_policy"] == POLICY


def test_false_positives_tighten_head_up_ratio():
    # 10 false positives whose average head_up (0.30) is above the current
    # threshold (0.38*0.5 = 0.19).  Suggestion must lower the ratio.
    stats = [
        _bucket("head_down", "false_positive", 10, 0.30, 0.34, 160, 150),
        _bucket("head_down", "true_positive", 8, 0.10, 0.12, 150, 140),
    ]
    result = suggest(stats, BASE, POLICY)
    assert result["confidence"] == "medium"
    assert "head_up_ratio" in result["changes"]
    assert result["changes"]["head_up_ratio"] < POLICY["head_up_ratio"]
    # Strictest true positive (0.12/0.38 ≈ 0.316) is the relaxation ceiling;
    # any suggestion stays within bounds.
    low, high = THRESHOLD_OVERRIDE_BOUNDS["head_up_ratio"]
    assert low <= result["changes"]["head_up_ratio"] <= high
    assert result["current_thresholds"]["head_up"] > result["suggested_thresholds"]["head_up"]


def test_false_positives_tighten_head_fwd_drop():
    # Lean false positives at avg fwd=145; current fwd threshold = 164-15=149,
    # so these are below threshold and did trigger.  Suggestion must increase
    # the drop (lower the threshold) so they stop firing.
    stats = [
        _bucket("lean", "false_positive", 12, 0.3, 0.35, 145.0, 143.0),
        _bucket("lean", "true_positive", 10, 0.2, 0.25, 130.0, 120.0),
    ]
    result = suggest(stats, BASE, POLICY)
    assert "head_fwd_drop" in result["changes"]
    assert result["changes"]["head_fwd_drop"] > POLICY["head_fwd_drop"]
    assert result["suggested_thresholds"]["head_fwd"] < result["current_thresholds"]["head_fwd"]


def test_suggestion_is_advisory_only():
    # The function must never mutate the supplied policy dict.
    policy = dict(POLICY)
    stats = [_bucket("head_down", "false_positive", 30, 0.30, 0.34, 160, 150)]
    suggest(stats, BASE, policy)
    assert policy == POLICY


def test_no_baseline_is_handled_by_service(tmp_path):
    from dataclasses import replace

    from posture.service import PostureService

    config = replace(Settings(), rtsp_url="", data_dir=tmp_path)
    service = PostureService(config)
    result = service.tuning_suggestion()
    assert result["error"] == "no_baseline"
    service.close()


def _missed_bucket(issue, count, up_avg, fwd_avg):
    return {
        "issue": issue,
        "count": count,
        "head_up": {"avg": up_avg, "min": up_avg, "max": up_avg},
        "head_fwd": {"avg": fwd_avg, "min": fwd_avg, "max": fwd_avg},
        "s_norm": {"avg": 0.09, "min": 0.08, "max": 0.1},
    }


def test_missed_events_relax_head_up_ratio():
    # Missed head-down at head_up=0.25 (above current threshold 0.19):
    # the trigger was too strict -> ratio should rise.
    missed = [_missed_bucket("head_down", 8, 0.25, 155.0)]
    result = suggest([], BASE, POLICY, missed_stats=missed)
    assert result["missed_total"] == 8
    assert result["missed_by_issue"]["head_down"] == 8
    assert "head_up_ratio" in result["changes"]
    assert result["changes"]["head_up_ratio"] > POLICY["head_up_ratio"]
    assert result["suggested_thresholds"]["head_up"] > result["current_thresholds"]["head_up"]


def test_missed_events_relax_head_fwd_drop():
    # Missed lean at fwd=155 (above threshold 149) -> smaller drop.
    missed = [_missed_bucket("lean", 10, 0.3, 155.0)]
    result = suggest([], BASE, POLICY, missed_stats=missed)
    assert "head_fwd_drop" in result["changes"]
    assert result["changes"]["head_fwd_drop"] < POLICY["head_fwd_drop"]
    assert result["suggested_thresholds"]["head_fwd"] > result["current_thresholds"]["head_fwd"]


def test_false_positive_and_miss_conflict_keeps_value():
    # A false positive asks to tighten head_up_ratio while a miss asks to
    # relax it; the knob must stay at its current value with a conflict note.
    stats = [
        _bucket("head_down", "false_positive", 10, 0.30, 0.34, 160, 150),
        _bucket("head_down", "true_positive", 8, 0.10, 0.12, 150, 140),
    ]
    missed = [_missed_bucket("head_down", 8, 0.25, 155.0)]
    result = suggest(stats, BASE, POLICY, missed_stats=missed)
    assert "head_up_ratio" not in result["changes"]
    assert any("低头" in c for c in result["conflicts"])
    assert any("冲突" in r for r in result["reasons"])


def test_missed_alone_counts_toward_confidence():
    missed = [_missed_bucket("head_down", 7, 0.25, 155.0)]
    result = suggest([], BASE, POLICY, missed_stats=missed)
    assert result["confidence"] == "medium"
