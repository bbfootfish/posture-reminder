"""Human verdict storage and service-level operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from posture.config import Settings
from posture.models import Metrics, Reminder
from posture.persistence import (
    VERDICT_FALSE,
    VERDICT_TRUE,
    ReminderStore,
)
from posture.service import PostureService


def _reminder(at: datetime) -> Reminder:
    return Reminder(
        at=at,
        issue="head_down",
        variant=0,
        text="头抬高一点点",
        phrase_file="x.mp3",
        metrics=Metrics(s_norm=0.08, head_up=0.12, head_fwd=155.0),
    )


def test_verdict_columns_migrate_and_roundtrip(tmp_path):
    store = ReminderStore(tmp_path / "r.sqlite3")
    store.record(_reminder(datetime(2026, 11, 20, 19, 0, 0)))
    row = store.recent_since(datetime(2026, 11, 20), 10)[0]
    assert row["id"] == 1
    assert row["verdict"] is None
    assert row["verdict_at"] is None
    assert row["verdict_note"] is None

    updated = store.set_verdict(1, VERDICT_FALSE, "其实坐得挺好")
    assert updated["verdict"] == VERDICT_FALSE
    assert updated["verdict_note"] == "其实坐得挺好"
    assert updated["verdict_at"]

    # Re-markable: change to true, then clear.
    assert store.set_verdict(1, VERDICT_TRUE)["verdict"] == VERDICT_TRUE
    cleared = store.set_verdict(1, None)
    assert cleared["verdict"] is None
    assert cleared["verdict_at"] is None
    assert cleared["verdict_note"] is None
    store.close()


def test_invalid_verdict_and_missing_id(tmp_path):
    store = ReminderStore(tmp_path / "r.sqlite3")
    store.record(_reminder(datetime(2026, 11, 20, 19, 0, 0)))
    with pytest.raises(ValueError):
        store.set_verdict(1, "maybe")
    assert store.set_verdict(999, VERDICT_TRUE) is None
    store.close()


def test_verdict_stats_only_count_labelled(tmp_path):
    store = ReminderStore(tmp_path / "r.sqlite3")
    for index, issue in enumerate(["head_down", "head_down", "lean"]):
        store.record(
            replace(_reminder(datetime(2026, 11, 20, 19, index, 0)), issue=issue)
        )
    store.set_verdict(1, VERDICT_TRUE)
    store.set_verdict(2, VERDICT_FALSE)
    stats = store.verdict_stats()
    assert len(stats) == 2
    by_key = {(s["issue"], s["verdict"]): s["count"] for s in stats}
    assert by_key[("head_down", VERDICT_TRUE)] == 1
    assert by_key[("head_down", VERDICT_FALSE)] == 1
    store.close()


def test_service_set_verdict_and_apply_thresholds(tmp_path):
    import json

    config = replace(Settings(), rtsp_url="", data_dir=tmp_path)
    service = PostureService(config)
    service._store.record(_reminder(datetime(2026, 11, 20, 19, 0, 0)))

    row = service.set_verdict(1, VERDICT_TRUE, "确认低头")
    assert row["verdict"] == VERDICT_TRUE
    with pytest.raises(KeyError):
        service.set_verdict(999, VERDICT_TRUE)
    with pytest.raises(ValueError):
        service.set_verdict(1, "bad")

    result = service.apply_thresholds({"head_up_ratio": 0.45, "head_fwd_drop": 14.0})
    assert result["restart_required"] is True
    data = json.loads((tmp_path / "thresholds_override.json").read_text(encoding="utf-8"))
    assert data["values"]["head_up_ratio"] == 0.45

    # Clearing writes an empty values object (env defaults restored on start).
    service.apply_thresholds({})
    data = json.loads((tmp_path / "thresholds_override.json").read_text(encoding="utf-8"))
    assert data["values"] == {}

    with pytest.raises(ValueError):
        service.apply_thresholds({"head_up_ratio": 99.0})
    service.close()


def test_invalid_verdict_excluded_from_tuning_stats(tmp_path):
    """An 'invalid' verdict marks a junk event (e.g. empty chair) and must
    never feed threshold tuning."""
    from posture.persistence import VERDICT_INVALID

    store = ReminderStore(tmp_path / "r.sqlite3")
    store.record(_reminder(datetime(2026, 11, 20, 19, 0, 0)))
    store.set_verdict(1, VERDICT_FALSE)   # real false positive
    store.record(_reminder(datetime(2026, 11, 20, 19, 1, 0)))
    store.set_verdict(2, VERDICT_INVALID)  # junk event

    stats = store.verdict_stats()
    verdicts = [s["verdict"] for s in stats]
    assert VERDICT_FALSE in verdicts
    assert VERDICT_INVALID not in verdicts
    assert sum(s["count"] for s in stats) == 1
    store.close()


def test_invalid_is_a_settable_verdict(tmp_path):
    from posture.persistence import VERDICT_INVALID

    store = ReminderStore(tmp_path / "r.sqlite3")
    store.record(_reminder(datetime(2026, 11, 20, 19, 0, 0)))
    row = store.set_verdict(1, VERDICT_INVALID, "无人画面")
    assert row["verdict"] == VERDICT_INVALID
    assert row["verdict_note"] == "无人画面"
    store.close()
