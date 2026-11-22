"""The replay CLI must reproduce the ticket's sample timeline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("POSTURE_") and key != "WORK_HOURS"
    }


def test_replay_sample_metrics_csv():
    env = _clean_env()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "posture.replay",
            "--csv",
            "sample_metrics.csv",
            "--json",
            "--ignore-schedule",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["samples"] > 300
    assert len(payload["reminders"]) == 1
    reminder = payload["reminders"][0]
    assert reminder["issue"] == "head_down"
    assert reminder["text"] in {"头抬高一点点", "把背直起来一点点"}
    from datetime import datetime

    offset = (
        datetime.fromisoformat(reminder["at"]) - datetime(2026, 11, 13, 16, 0, 0)
    ).total_seconds()
    assert 40 <= offset <= 75


def test_replay_without_ignore_schedule_drops_blocked_samples():
    """2026-11-13 是周五；14:00 起落在上学屏蔽段，不应产出提醒。"""
    env = _clean_env()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "posture.replay",
            "--csv",
            "sample_metrics.csv",
            "--json",
            "--start",
            "2026-11-13T14:00:00",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["reminders"] == []