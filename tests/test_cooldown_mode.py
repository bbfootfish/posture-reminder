"""Cooldown policy: unlimited in debug, at least 5 minutes in production."""

from __future__ import annotations

import os

from posture.config import Settings

KEY_DEBUG = "POSTURE_DEBUG_MODE"
KEY_COOLDOWN = "POSTURE_COOLDOWN_SECONDS"
KEY_DATA = "POSTURE_DATA_DIR"


def _env(monkeypatch, *, debug: str | None, cooldown: str | None, tmp_path):
    for key in (KEY_DEBUG, KEY_COOLDOWN):
        monkeypatch.delenv(key, raising=False)
    if debug is not None:
        monkeypatch.setenv(KEY_DEBUG, debug)
    if cooldown is not None:
        monkeypatch.setenv(KEY_COOLDOWN, cooldown)
    monkeypatch.setenv(KEY_DATA, str(tmp_path))


def test_debug_mode_disables_cooldown(monkeypatch, tmp_path):
    _env(monkeypatch, debug="1", cooldown="600", tmp_path=tmp_path)
    settings = Settings.from_env()
    assert settings.debug_mode is True
    assert settings.cooldown_seconds == 0.0


def test_production_clamps_cooldown_to_at_least_five_minutes(monkeypatch, tmp_path):
    _env(monkeypatch, debug="0", cooldown="60", tmp_path=tmp_path)
    settings = Settings.from_env()
    assert settings.debug_mode is False
    assert settings.cooldown_seconds == 300.0


def test_production_keeps_larger_cooldown(monkeypatch, tmp_path):
    _env(monkeypatch, debug="0", cooldown="600", tmp_path=tmp_path)
    settings = Settings.from_env()
    assert settings.cooldown_seconds == 600.0


def test_default_is_production_and_hourly_cap_unlimited(monkeypatch, tmp_path):
    _env(monkeypatch, debug=None, cooldown=None, tmp_path=tmp_path)
    settings = Settings.from_env()
    assert settings.debug_mode is False
    assert settings.cooldown_seconds == 300.0
    assert settings.hourly_cap == 0
