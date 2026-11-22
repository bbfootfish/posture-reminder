"""Tests for tools/speak.py DLNA playback lifecycle.

speak.py is a standalone script (not an importable package), so it is loaded
by path.  These tests cover the Play -> wait -> Stop sequence that prevents
the Xiaomi DLNA renderer from looping a phrase forever.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture()
def speak_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "speak.py"
    spec = importlib.util.spec_from_file_location("speak_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_speak(speak_tool: ModuleType, monkeypatch, extra_args: list[str]):
    calls: list[tuple[str, str]] = []

    def fake_post(control_url, service_type, action, body, timeout):
        calls.append(("soap", action))

    monkeypatch.setattr(speak_tool, "_post_soap", fake_post)
    slept: list[float] = []
    monkeypatch.setattr(speak_tool.time, "sleep", lambda seconds: slept.append(seconds))

    rc = speak_tool.main(
        [
            "--url",
            "http://host:8090/head_down_1.mp3",
            "--control-url",
            "http://renderer:9999/AVTransport/control",
            *extra_args,
        ]
    )
    return rc, calls, slept


def test_play_then_stop_sequence(speak_tool, monkeypatch):
    """A reminder must SetURI, Play, wait, then Stop so the speaker never loops."""
    rc, calls, slept = _run_speak(speak_tool, monkeypatch, [])

    assert rc == 0
    actions = [a for kind, a in calls if kind == "soap"]
    assert actions == ["SetAVTransportURI", "Play", "Stop"]
    assert slept == [pytest.approx(2.5)]


def test_stop_after_zero_disables_auto_stop(speak_tool, monkeypatch):
    """--stop-after 0 keeps the old behaviour: no wait and no Stop."""
    rc, calls, slept = _run_speak(speak_tool, monkeypatch, ["--stop-after", "0"])

    assert rc == 0
    actions = [a for kind, a in calls if kind == "soap"]
    assert actions == ["SetAVTransportURI", "Play"]
    assert slept == []


def test_custom_stop_after_is_used(speak_tool, monkeypatch):
    """--stop-after controls the wait before Stop."""
    rc, calls, slept = _run_speak(speak_tool, monkeypatch, ["--stop-after", "4.0"])

    assert rc == 0
    assert slept == [pytest.approx(4.0)]
    actions = [a for kind, a in calls if kind == "soap"]
    assert actions[-1] == "Stop"


def test_stop_sent_even_when_volume_set(speak_tool, monkeypatch):
    """With volume control the order is SetURI, Play, SetVolume, wait, Stop."""
    speak_calls: list[str] = []

    def fake_post(control_url, service_type, action, body, timeout):
        speak_calls.append(action)

    monkeypatch.setattr(speak_tool, "_post_soap", fake_post)
    slept: list[float] = []
    monkeypatch.setattr(speak_tool.time, "sleep", lambda seconds: slept.append(seconds))

    rc = speak_tool.main(
        [
            "--url",
            "http://host:8090/head_down_1.mp3",
            "--control-url",
            "http://renderer:9999/AVTransport/control",
            "--volume-control-url",
            "http://renderer:9999/RenderingControl/control",
            "--volume",
            "35",
        ]
    )

    assert rc == 0
    assert speak_calls == ["SetAVTransportURI", "Play", "SetVolume", "Stop"]
    assert len(slept) == 1
