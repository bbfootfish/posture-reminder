"""Blocked hours must resolve to the right mode before inference runs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from posture.config import Settings
from posture.schedule import Mode
from posture.service import PostureService


class _Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _service(tmp_path, at: datetime) -> PostureService:
    settings = replace(
        Settings(),
        rtsp_url="rtsp://127.0.0.1:0/none",
        data_dir=tmp_path,
        api_host="127.0.0.1",
        api_port=0,
    )
    return PostureService(settings, clock=_Clock(at))


def test_blocked_moment_reports_blocked_mode(tmp_path):
    at = datetime(2026, 11, 13, 10, 0)  # 周五上午，上学时段
    service = _service(tmp_path, at)
    try:
        assert service.current_mode() is Mode.BLOCK
    finally:
        service.close()


def test_monitor_only_moment_reports_monitor_only(tmp_path):
    at = datetime(2026, 11, 13, 22, 30)
    service = _service(tmp_path, at)
    try:
        assert service.current_mode() is Mode.MONITOR_ONLY
    finally:
        service.close()


def test_allowed_moment_reports_allow(tmp_path):
    at = datetime(2026, 11, 13, 19, 0)
    service = _service(tmp_path, at)
    try:
        assert service.current_mode() is Mode.ALLOW
    finally:
        service.close()