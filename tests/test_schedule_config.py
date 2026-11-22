"""Configuration wiring for the schedule: env parsing and break-file loading."""

from __future__ import annotations

import json
import logging

from posture.config import Settings, load_school_breaks


def test_settings_parse_block_windows_from_env(monkeypatch):
    monkeypatch.setenv("POSTURE_NIGHT_BLOCK", "00:00-08:00")
    monkeypatch.setenv("POSTURE_SCHOOL_BLOCK", "08:00-18:00")
    monkeypatch.setenv("POSTURE_LATE_NIGHT_BLOCK", "22:00-24:00")
    monkeypatch.delenv("WORK_HOURS", raising=False)
    settings = Settings.from_env()
    assert str(settings.schedule.night) == "00:00-08:00"
    assert str(settings.schedule.school) == "08:00-18:00"
    assert str(settings.schedule.late_night) == "22:00-24:00"


def test_load_school_breaks_reads_valid_file(tmp_path):
    path = tmp_path / "breaks.json"
    path.write_text(
        json.dumps(
            {
                "breaks": [
                    {"start": "2027-01-18", "end": "2027-02-20", "label": "寒假"},
                    {"start": "2027-07-01", "end": "2027-08-31", "label": "暑假"},
                ]
            }
        ),
        encoding="utf-8",
    )
    breaks = load_school_breaks(path)
    assert len(breaks) == 2
    assert breaks[0].label == "寒假"


def test_load_school_breaks_tolerates_missing_file(tmp_path):
    assert load_school_breaks(tmp_path / "absent.json") == ()


def test_load_school_breaks_tolerates_broken_json(tmp_path, caplog):
    path = tmp_path / "breaks.json"
    path.write_text("{ this is not json", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="posture.config")
    assert load_school_breaks(path) == ()
    assert any("school breaks" in r.getMessage().lower() for r in caplog.records)


def test_load_school_breaks_skips_invalid_entries(tmp_path, caplog):
    path = tmp_path / "breaks.json"
    path.write_text(
        json.dumps(
            {
                "breaks": [
                    {"start": "2027-02-20", "end": "2027-01-18", "label": "倒序"},
                    {"start": "2027-07-01", "end": "2027-08-31", "label": "暑假"},
                ]
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="posture.config")
    breaks = load_school_breaks(path)
    assert [b.label for b in breaks] == ["暑假"]


def test_settings_have_no_work_hours_attribute():
    assert not hasattr(Settings(), "work_hours")