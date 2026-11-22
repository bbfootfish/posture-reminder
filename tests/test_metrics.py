"""Metric extraction from synthetic landmarks (no MediaPipe needed)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from posture.metrics import extract_sample, metrics_from_landmarks


def landmark(x: float, y: float, visibility: float = 0.9):
    return SimpleNamespace(x=x, y=y, z=0.0, visibility=visibility)


def make_landmarks(**overrides):
    # 33 default points; overwrite only the ones the metrics use.
    points = [landmark(0.5, 0.5) for _ in range(33)]
    points[0] = landmark(0.5, 0.4)  # nose
    points[7] = landmark(0.49, 0.35)  # left ear
    points[8] = landmark(0.50, 0.35)  # right ear
    points[11] = landmark(0.45, 0.60)  # left shoulder
    points[12] = landmark(0.5375, 0.60)  # right shoulder
    points[23] = landmark(0.49, 0.90)  # left hip
    points[24] = landmark(0.50, 0.90)  # right hip
    for index, point in overrides.items():
        points[index] = point
    return points


def test_s_norm_and_head_up_use_pixel_values():
    sample = extract_sample(make_landmarks(), 1920, 1080)
    assert sample is not None
    assert sample.shoulder_px == pytest.approx(0.0875 * 1920)
    assert sample.metrics.s_norm == pytest.approx(0.0875)
    assert sample.metrics.head_up == pytest.approx((0.60 - 0.40) * 1080 / 168)
    assert sample.ear_shoulder_ratio > 0
    assert sample.hip_visible is True


def test_head_fwd_is_a_180_degree_angle_when_standing_straight():
    points = make_landmarks()
    # Ear directly above the shoulder and hip directly below it.
    points[11] = landmark(0.45, 0.60)
    points[23] = landmark(0.45, 0.90)
    points[7] = landmark(0.45, 0.30)
    sample = extract_sample(points, 1920, 1080)
    assert sample is not None
    assert sample.metrics.head_fwd == pytest.approx(180.0, abs=1e-6)


def test_head_fwd_is_135_degrees_for_a_45_degree_ear_offset():
    points = make_landmarks()
    points[11] = landmark(0.45, 0.60)
    points[7] = landmark(0.45, 0.35)
    # 270 px right and 270 px down from the shoulder.
    points[23] = landmark(0.45 + 270 / 1920, 0.60 + 270 / 1080)
    sample = extract_sample(points, 1920, 1080)
    assert sample is not None
    assert sample.metrics.head_fwd == pytest.approx(135.0, abs=1e-6)


def test_invisible_keypoints_are_rejected():
    points = make_landmarks()
    points[0] = landmark(0.5, 0.4, visibility=0.5)
    assert metrics_from_landmarks(points, 1920, 1080) is None

    points = make_landmarks()
    points[7] = landmark(0.49, 0.35, visibility=0.1)
    points[23] = landmark(0.49, 0.9, visibility=0.1)
    # The right side is visible, so the sample should still be usable.
    sample = extract_sample(points, 1920, 1080)
    assert sample is not None


def test_narrow_shoulders_are_rejected():
    points = make_landmarks()
    points[11] = landmark(0.45, 0.60)
    points[12] = landmark(0.455, 0.60)  # 9.6 px
    assert extract_sample(points, 1920, 1080) is None


def test_empty_or_tiny_frames_are_rejected():
    assert extract_sample(None, 1920, 1080) is None
    assert extract_sample(make_landmarks(), 0, 1080) is None


def test_diagnostics_separate_detection_from_metric_validity():
    from posture.metrics import diagnostics_from_landmarks

    points = make_landmarks()
    points[23] = landmark(0.49, 0.90, visibility=0.10)
    points[24] = landmark(0.50, 0.90, visibility=0.10)

    # The metric path rejects the frame because the hip angle is unreliable...
    assert extract_sample(points, 1920, 1080) is None
    # ...but the audit still sees a detected pose and records hidden hips.
    diagnostics = diagnostics_from_landmarks(points, 1920, 1080)
    assert diagnostics.detected is True
    assert diagnostics.hip_visible is False
    assert diagnostics.head_fwd is None
    assert diagnostics.shoulder_width_pct == pytest.approx(8.75)


def test_head_below_shoulders_is_rejected():
    """A nose below the shoulder line is not a seated person.

    MediaPipe emits such landmark sets on empty chairs / off-frame subjects;
    head_up becomes negative, which is impossible for the side-view geometry
    this service judges.  These frames must never reach the state machine.
    """
    points = make_landmarks()
    points[0] = landmark(0.5, 0.75)  # nose below the shoulders at y=0.60
    assert extract_sample(points, 1920, 1080) is None
    assert metrics_from_landmarks(points, 1920, 1080) is None


def test_false_pose_with_tiny_shoulders_is_rejected():
    """Empty-chair false detections have ~2% shoulder width, not ~8%.

    The ticket's real subject measured 8.7% of frame width; a pose detected on
    an empty chair measured 2.16% (20.7 px at 960 wide).  A purely absolute
    20 px gate lets these through, so the width must scale with the frame.
    """
    # 960-wide frame, shoulders 20 px apart -> ~2.1% of width.
    pts = make_landmarks()
    pts[11] = landmark(0.45, 0.60)
    pts[12] = landmark(0.45 + 20 / 960, 0.60)
    assert extract_sample(pts, 960, 540, min_shoulder_pct=6.0) is None


def test_real_subject_shoulder_width_passes():
    """8.7% shoulder width (the ticket's measured value) must still pass."""
    pts = make_landmarks()
    pts[11] = landmark(0.45, 0.60)
    pts[12] = landmark(0.45 + 0.087, 0.60)
    sample = extract_sample(pts, 1920, 1080, min_shoulder_pct=6.0)
    assert sample is not None
    assert sample.metrics.s_norm == pytest.approx(0.087, abs=1e-3)


