"""Camera audit report with synthetic pose samples."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from posture.audit import AuditAccumulator, AuditObservation, classify_view
from posture.config import Settings

from .helpers import START, settings


def frame(
    head_fwd: float | None,
    *,
    ratio: float | None = 0.25,
    hip_visible: bool = True,
    shoulder_pct: float | None = 8.7,
    detected: bool = True,
) -> AuditObservation:
    return AuditObservation(
        at=START,
        detected=detected,
        hip_visible=hip_visible if detected else None,
        ear_shoulder_ratio=ratio,
        shoulder_width_pct=shoulder_pct,
        head_fwd=head_fwd,
    )


def accumulate(observations) -> AuditAccumulator:
    accumulator = AuditAccumulator(settings())
    for index, item in enumerate(observations):
        accumulator.add(replace(item, at=START + timedelta(seconds=index * 0.2)))
    return accumulator


def test_good_side_view_passes_every_check():
    samples = [frame(164.0 + (index - 49.5) * 0.32) for index in range(100)]
    report = accumulate(samples).report()
    assert report.passed is True
    assert report.view == "side"
    assert report.detection_rate == 1.0
    assert report.shoulder_width_pct == 8.7
    assert report.hip_visible_rate == 1.0
    assert report.head_fwd_jitter_deg <= 2.5
    assert report.head_fwd_range_5s_deg >= 5.0
    assert all(report.checks.values())
    assert report.reasons == []


def test_frontal_view_fails():
    samples = [frame(164.0, ratio=0.65) for _ in range(100)]
    report = accumulate(samples).report()
    assert report.view == "frontal"
    assert report.passed is False
    assert report.checks["view_is_side"] is False
    assert any("side view" in reason for reason in report.reasons)


def test_low_detection_rate_fails():
    samples = [frame(164.0)] * 50 + [frame(None, detected=False) for _ in range(50)]
    report = accumulate(samples).report()
    assert report.detection_rate == 0.5
    assert report.checks["detection_rate"] is False
    assert report.passed is False


def test_narrow_shoulders_fail():
    samples = [frame(164.0, shoulder_pct=5.0) for _ in range(100)]
    report = accumulate(samples).report()
    assert report.checks["shoulder_width"] is False
    assert report.passed is False


def test_hidden_hips_fail():
    samples = [frame(164.0, hip_visible=False) for _ in range(100)]
    report = accumulate(samples).report()
    assert report.hip_visible_rate == 0.0
    assert report.checks["hip_visible"] is False
    assert report.passed is False


def test_noisy_head_fwd_fails_stability():
    # Drive the jitter well past the configured ceiling instead of hard-coding
    # a magnitude: the threshold is an installation-tunable setting (raised to
    # 6 deg for a live camera where a drawing child moves constantly), and a
    # fixed input would silently stop testing anything once it changes.
    limit = Settings().audit_max_jitter_deg
    samples = [frame(164.0 if index % 2 == 0 else 164.0 + limit * 2) for index in range(100)]
    report = accumulate(samples).report()
    assert report.head_fwd_jitter_deg > limit
    assert report.checks["head_fwd_stable"] is False
    assert report.passed is False


def test_flat_head_fwd_fails_signal_check():
    samples = [frame(164.0) for _ in range(100)]
    report = accumulate(samples).report()
    assert report.head_fwd_range_5s_deg == 0.0
    assert report.checks["head_fwd_signal"] is False
    assert report.passed is False


def test_no_frames_fails():
    report = AuditAccumulator(settings()).report()
    assert report.passed is False
    assert report.view == "unknown"
    assert report.reasons


def test_classify_view_ranges():
    # Derived from the module constants: this installation's rear-quarter camera
    # measured 0.30-0.41, so the side band was widened from (0.15, 0.35) to
    # (0.15, 0.45).  Hard-coding boundary values made the test break on every
    # retune without testing anything new.
    from posture.audit import FRONTAL_RATIO_RANGE, SIDE_RATIO_RANGE

    side_lo, side_hi = SIDE_RATIO_RANGE
    frontal_lo, frontal_hi = FRONTAL_RATIO_RANGE
    assert classify_view((side_lo + side_hi) / 2) == "side"
    assert classify_view((frontal_lo + frontal_hi) / 2) == "frontal"
    assert classify_view(side_hi + 0.01) == "unknown"      # just outside side
    assert classify_view(frontal_hi + 0.01) == "unknown"   # beyond frontal
    assert classify_view(None) == "unknown"


def test_short_audit_window_fails_duration_check():
    samples = [frame(164.0 + index * 0.1) for index in range(50)]
    report = accumulate(samples).report(min_duration_seconds=16.0)
    assert report.checks["duration"] is False
    assert report.passed is False
    assert any("shorter" in reason for reason in report.reasons)
