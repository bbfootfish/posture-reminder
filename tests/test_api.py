"""HTTP API smoke test: healthz works without a camera or model."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from posture.config import Settings
from posture.service import PostureService


def test_healthz_without_camera_or_baseline(tmp_path):
    config = replace(
        Settings(),
        rtsp_url="",
        data_dir=tmp_path,
        api_host="127.0.0.1",
        api_port=0,
    )
    service = PostureService(config)
    port = service.start_api()
    base = f"http://127.0.0.1:{port}"
    try:
        with urlopen(base + "/healthz", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("application/json")
            payload = json.load(response)
        assert payload["status"] == "degraded"
        assert payload["state"] == "unconfigured"
        assert payload["baseline_present"] is False
        assert payload["camera_audit_passed"] is None
        assert payload["stream_connected"] is False
        assert "08:00-18:00" in payload["work_hours"]
        assert payload["schedule_mode"] in {"block", "monitor_only", "allow"}
        assert payload["calendar_ok"] is True
        assert payload["calendar_supported_years"] == [2004, 2027]
        assert isinstance(payload["skipped_blocked_frames"], int)
        assert "fps" in payload and "reminders_today" in payload

        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/calibrate", method="POST"), timeout=5)
        assert error.value.code == 503

        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/audit", method="POST"), timeout=5)
        assert error.value.code == 503

        with pytest.raises(HTTPError) as error:
            urlopen(base + "/nope", timeout=5)
        assert error.value.code == 404
    finally:
        service.close()


def test_healthz_reports_baseline_when_present(tmp_path):
    from posture.baseline import BaselineStore
    from posture.models import Baseline

    BaselineStore(tmp_path / "baseline.json").save(
        Baseline(s_norm=0.0875, head_up=0.38, head_fwd=164.0), samples=41
    )
    config = replace(
        Settings(),
        rtsp_url="",
        data_dir=tmp_path,
        api_host="127.0.0.1",
        api_port=0,
    )
    service = PostureService(config)
    port = service.start_api()
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            payload = json.load(response)
        assert payload["baseline_present"] is True
        assert payload["baseline"]["head_up"] == 0.38
        assert payload["state"] == "unconfigured"
    finally:
        service.close()


def test_verdict_and_tuning_endpoints(tmp_path):
    from posture.baseline import BaselineStore
    from posture.models import Baseline, Metrics, Reminder

    BaselineStore(tmp_path / "baseline.json").save(
        Baseline(s_norm=0.0875, head_up=0.38, head_fwd=164.0), samples=41
    )
    config = replace(
        Settings(),
        rtsp_url="",
        data_dir=tmp_path,
        api_host="127.0.0.1",
        api_port=0,
    )
    service = PostureService(config)
    service._store.record(
        Reminder(
            at=datetime(2026, 11, 20, 19, 0, 0),
            issue="head_down",
            variant=0,
            text="头抬高一点点",
            phrase_file="x.mp3",
            metrics=Metrics(s_norm=0.08, head_up=0.12, head_fwd=155.0),
        )
    )
    port = service.start_api()
    base = f"http://127.0.0.1:{port}"
    try:
        # POST a verdict.
        req = Request(
            base + "/api/alert-verdict",
            data=json.dumps({"id": 1, "verdict": "false_positive", "note": "误报"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=5) as response:
            payload = json.load(response)
        assert payload["ok"] is True
        assert payload["reminder"]["verdict"] == "false_positive"

        # Invalid verdict -> 400.
        bad = Request(
            base + "/api/alert-verdict",
            data=json.dumps({"id": 1, "verdict": "wrong"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(bad, timeout=5)
        assert error.value.code == 400

        # GET tuning + policy.
        with urlopen(base + "/api/tuning", timeout=5) as response:
            tuning = json.load(response)
        assert "changes" in tuning and "confidence" in tuning
        with urlopen(base + "/api/policy", timeout=5) as response:
            policy = json.load(response)
        assert "head_up_ratio" in policy

        # Apply thresholds.
        apply = Request(
            base + "/api/apply-thresholds",
            data=json.dumps({"values": {"head_up_ratio": 0.45}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(apply, timeout=5) as response:
            applied = json.load(response)
        assert applied["restart_required"] is True
        assert applied["applied"]["head_up_ratio"] == 0.45
    finally:
        service.close()


def test_settings_loads_threshold_override(tmp_path):
    import os
    from posture.config import Settings

    (tmp_path / "thresholds_override.json").write_text(
        json.dumps({"values": {"head_up_ratio": 0.42, "head_fwd_drop": 12.0}}),
        encoding="utf-8",
    )
    old = os.environ.get("POSTURE_DATA_DIR")
    os.environ["POSTURE_DATA_DIR"] = str(tmp_path)
    try:
        settings = Settings.from_env()
        assert settings.head_up_ratio == 0.42
        assert settings.head_fwd_drop == 12.0
        assert settings.occupancy_threshold == 0.55  # untouched
    finally:
        if old is None:
            os.environ.pop("POSTURE_DATA_DIR", None)
        else:
            os.environ["POSTURE_DATA_DIR"] = old


def test_missed_events_endpoints(tmp_path):
    config = replace(
        Settings(),
        rtsp_url="",
        data_dir=tmp_path,
        api_host="127.0.0.1",
        api_port=0,
    )
    service = PostureService(config)
    port = service.start_api()
    base = f"http://127.0.0.1:{port}"

    def post(obj):
        req = Request(
            base + "/api/missed-events",
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)

    try:
        # Add — timestamp relative to now: the list endpoint queries "last N
        # hours from now", so a hard-coded date silently falls out of the
        # window as the calendar moves.
        recent = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        s, r = post({
            "action": "add",
            "at": recent,
            "issue": "head_down",
            "note": "test",
        })
        assert s == 201
        eid = r["event"]["id"]
        assert r["event"]["issue"] == "head_down"

        # List
        with urlopen(base + "/api/missed-events?hours=24", timeout=5) as response:
            data = json.load(response)
        assert len(data["events"]) == 1

        # Update
        s, r = post({"action": "update", "id": eid, "issue": "lean", "note": "edited"})
        assert r["event"]["issue"] == "lean"
        assert r["event"]["note"] == "edited"

        # Invalid action
        bad = Request(
            base + "/api/missed-events",
            data=json.dumps({"action": "nope"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(bad, timeout=5)
        assert error.value.code == 400

        # Delete
        s, r = post({"action": "delete", "id": eid})
        assert r["ok"] is True
        with urlopen(base + "/api/missed-events", timeout=5) as response:
            assert json.load(response)["events"] == []
    finally:
        service.close()


def test_timeline_endpoint_returns_requested_window(tmp_path):
    from datetime import timedelta

    config = replace(
        Settings(),
        rtsp_url="",
        data_dir=tmp_path,
        api_host="127.0.0.1",
        api_port=0,
    )
    service = PostureService(config)
    base = datetime(2026, 11, 20, 18, 0, 0)
    for i in range(6):
        service._monitor.maybe_add(
            base + timedelta(seconds=i),
            {"smoothed": {"s_norm": 0.07, "head_up": 0.3, "head_fwd": 150.0},
             "status": "ok", "issue": None, "window": {"occupancy": 0.0}},
            person=True,
        )
    port = service.start_api()
    base_url = f"http://127.0.0.1:{port}"
    try:
        url = (
            base_url + "/api/timeline?start=" + base.isoformat()
            + "&end=" + (base + timedelta(seconds=5)).isoformat()
            + "&bucket_seconds=1"
        )
        with urlopen(url, timeout=5) as response:
            payload = json.load(response)
        assert payload["bucket_seconds"] == 1.0
        assert len(payload["buckets"]) == 6
        assert payload["buckets"][0]["samples"] == 1

        with pytest.raises(HTTPError) as error:
            urlopen(base_url + "/api/timeline?start=bad&end=bad", timeout=5)
        assert error.value.code == 400
    finally:
        service.close()
