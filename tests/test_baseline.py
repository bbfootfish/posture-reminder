"""Baseline median, JSON round-trip and calibration errors."""

from __future__ import annotations

from datetime import timedelta

import pytest

from posture.baseline import (
    BaselineError,
    BaselineStore,
    CalibrationSession,
)
from posture.models import Baseline, Metrics

from .helpers import START


def test_calibration_uses_median_not_mean():
    session = CalibrationSession(duration_seconds=1.0, min_samples=4)
    values = [
        Metrics(0.080, 0.30, 160.0),
        Metrics(0.087, 0.38, 164.0),
        Metrics(0.090, 0.42, 166.0),
        Metrics(0.999, 9.99, 999.0),  # one glitch must not move the median
    ]
    for index, metrics in enumerate(values):
        session.add(metrics, START + timedelta(seconds=index * 0.1))
    assert session.add(None, START + timedelta(seconds=1.0)) is True
    result = session.result()
    assert result.samples == 4
    assert result.baseline.head_up == pytest.approx(0.40)
    assert result.baseline.head_fwd == pytest.approx(165.0)
    assert result.baseline.s_norm == pytest.approx(0.0885)


def test_calibration_rejects_too_few_samples():
    session = CalibrationSession(duration_seconds=0.5, min_samples=5)
    session.add(Metrics(0.087, 0.38, 164.0), START)
    session.add(None, START + timedelta(seconds=0.6))
    with pytest.raises(BaselineError):
        session.result()


def test_baseline_store_round_trip(tmp_path):
    store = BaselineStore(tmp_path / "baseline.json")
    assert store.load() is None
    baseline = Baseline(s_norm=0.0875, head_up=0.38, head_fwd=164.0)
    store.save(baseline, samples=41)
    loaded = store.load()
    assert loaded == baseline
    raw = (tmp_path / "baseline.json").read_text(encoding="utf-8")
    assert '"samples": 41' in raw
    assert '"source": "calibrate"' in raw


def test_corrupt_baseline_raises(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BaselineError):
        BaselineStore(path).load()
