"""Missed-event marking: storage, service, snapshot and CRUD."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from posture.config import Settings
from posture.models import Metrics
from posture.monitor import MonitorHistory
from posture.persistence import MissedEventStore
from posture.service import PostureService

MOMENT = datetime(2026, 11, 20, 18, 30, 0)


def _store(tmp_path) -> MissedEventStore:
    return MissedEventStore(tmp_path / "posture.sqlite3")


def test_add_update_delete_roundtrip(tmp_path):
    store = _store(tmp_path)
    row = store.add(MOMENT, "head_down", "写了半小时没提醒", 0.24, 152.0, 0.07)
    assert row["id"] == 1
    assert row["issue"] == "head_down"
    assert row["note"] == "写了半小时没提醒"
    assert row["head_up"] == 0.24
    assert row["at"].startswith("2026-11-20T18:30")

    updated = store.update(1, issue="lean", note="改成侧歪")
    assert updated["issue"] == "lean"
    assert updated["note"] == "改成侧歪"

    cleared = store.update(1, note="")
    assert cleared["note"] is None  # empty note stored as NULL

    assert store.delete(1) is True
    assert store.delete(1) is False
    assert store.list() == []
    store.close()


def test_list_since_and_ordering(tmp_path):
    store = _store(tmp_path)
    store.add(MOMENT, "head_down", None)
    store.add(MOMENT + timedelta(hours=3), "lean", None)
    all_rows = store.list()
    assert len(all_rows) == 2
    assert all_rows[0]["at"] > all_rows[1]["at"]  # newest first
    recent = store.list(since=MOMENT + timedelta(hours=1))
    assert len(recent) == 1
    assert recent[0]["issue"] == "lean"
    store.close()


def test_stats_group_by_issue(tmp_path):
    store = _store(tmp_path)
    store.add(MOMENT, "head_down", None, 0.24, 152.0, 0.07)
    store.add(MOMENT + timedelta(minutes=1), "head_down", None, 0.26, 154.0, 0.07)
    store.add(MOMENT + timedelta(minutes=2), "lean", None, 0.3, 150.0, 0.07)
    stats = store.stats()
    by_issue = {r["issue"]: r for r in stats}
    assert by_issue["head_down"]["count"] == 2
    assert by_issue["lean"]["count"] == 1
    assert by_issue["head_down"]["head_up"]["avg"] == pytest.approx(0.25)
    store.close()


def test_service_add_missed_fills_snapshot_from_samples(tmp_path):
    # Seed one monitor sample near MOMENT.
    monitor = MonitorHistory(tmp_path / "posture.sqlite3", retention_hours=48)
    snapshot = {
        "smoothed": {"s_norm": 0.07, "head_up": 0.23, "head_fwd": 151.0},
        "status": "out_of_line",
        "window": {"occupancy": 0.6},
    }
    monitor.maybe_add(MOMENT, snapshot, person=True)

    config = replace(Settings(), rtsp_url="", data_dir=tmp_path)
    service = PostureService(config)
    row = service.add_missed(MOMENT, "head_down", "漏报")
    assert row["issue"] == "head_down"
    assert row["head_up"] == pytest.approx(0.23)
    assert row["head_fwd"] == pytest.approx(151.0)
    assert row["s_norm"] == pytest.approx(0.07)

    service.close()
    monitor.close()


def test_service_missed_crud_and_invalid_issue(tmp_path):
    config = replace(Settings(), rtsp_url="", data_dir=tmp_path)
    service = PostureService(config)
    with pytest.raises(ValueError):
        service.add_missed(MOMENT, "bogus")
    row = service.add_missed(MOMENT, "lean")
    assert service.update_missed(row["id"], note="x")["note"] == "x"
    with pytest.raises(KeyError):
        service.update_missed(999, note="x")
    assert service.delete_missed(row["id"]) is True
    assert service.list_missed(hours=24) == []
    service.close()


def test_add_missed_accepts_explicit_snapshot(tmp_path):
    """The timeline passes bucket averages; they must be stored verbatim."""
    config = replace(Settings(), rtsp_url="", data_dir=tmp_path)
    service = PostureService(config)
    row = service.add_missed(
        MOMENT,
        "head_down",
        "回看时间线标记",
        snapshot={"head_up": 0.21, "head_fwd": 148.5, "s_norm": 0.075},
    )
    assert row["head_up"] == pytest.approx(0.21)
    assert row["head_fwd"] == pytest.approx(148.5)
    assert row["s_norm"] == pytest.approx(0.075)
    service.close()


def test_add_missed_falls_back_when_snapshot_incomplete(tmp_path):
    """A snapshot without usable numbers falls back to the nearest sample."""
    config = replace(Settings(), rtsp_url="", data_dir=tmp_path)
    service = PostureService(config)
    row = service.add_missed(
        MOMENT, "lean", None, snapshot={"head_up": None, "head_fwd": "bad"}
    )
    assert row["head_up"] is None  # no samples stored in this tmp DB
    service.close()
