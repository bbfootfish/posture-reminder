"""Automatic audit starts only after a seated person is present long enough."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from posture.config import Settings
from posture.models import Metrics
from posture.service import PostureService

from .helpers import START


def test_auto_audit_waits_for_presence_and_then_starts(tmp_path):
    config = replace(
        Settings(),
        data_dir=tmp_path,
        rtsp_url="",
        require_audit=True,
        auto_audit=True,
        auto_audit_presence_seconds=5.0,
        auto_audit_retry_seconds=600.0,
    )
    service = PostureService(config)
    sample = SimpleNamespace(metrics=Metrics(0.06, 0.38, 164.0))
    try:
        assert service._maybe_start_auto_audit(sample, START) is False
        assert service._maybe_start_auto_audit(sample, START + timedelta(seconds=4.8)) is False
        assert service._maybe_start_auto_audit(sample, START + timedelta(seconds=5.0)) is True
        assert service._audit is not None
        assert service._last_audit_attempt_at == START + timedelta(seconds=5.0)

        # A second attempt during the retry window is not started.
        service._audit = None
        assert service._maybe_start_auto_audit(sample, START + timedelta(seconds=10.0)) is False
    finally:
        service.close()


def test_auto_audit_ignores_a_lying_person(tmp_path):
    config = replace(
        Settings(),
        data_dir=tmp_path,
        rtsp_url="",
        auto_audit=True,
        auto_audit_presence_seconds=1.0,
    )
    service = PostureService(config)
    lying = SimpleNamespace(metrics=Metrics(0.06, -0.1, 174.0))
    try:
        assert service._maybe_start_auto_audit(lying, START + timedelta(seconds=5.0)) is False
        assert service._audit is None
    finally:
        service.close()


def test_auto_audit_can_be_disabled(tmp_path):
    config = replace(
        Settings(),
        data_dir=tmp_path,
        rtsp_url="",
        auto_audit=False,
        auto_audit_presence_seconds=1.0,
    )
    service = PostureService(config)
    sample = SimpleNamespace(metrics=Metrics(0.06, 0.38, 164.0))
    try:
        assert service._maybe_start_auto_audit(sample, START + timedelta(seconds=10.0)) is False
        assert service._audit is None
    finally:
        service.close()
