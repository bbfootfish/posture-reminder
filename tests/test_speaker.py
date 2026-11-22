"""Speaker command formatting and dry-run behaviour."""

from __future__ import annotations

from datetime import datetime

from posture.models import HEAD_DOWN, Metrics, Reminder
from posture.speaker import Speaker

from .helpers import settings


def make_reminder(variant: int = 0) -> Reminder:
    return Reminder(
        at=datetime(2026, 11, 13, 16, 10, 0),
        issue=HEAD_DOWN,
        variant=variant,
        text="头抬高一点点",
        phrase_file="head_down_1.mp3",
        metrics=Metrics(0.0875, 0.12, 155.0),
    )


def test_dry_run_has_no_command():
    speaker = Speaker(settings(speak_cmd="", media_base_url="http://host:8090"))
    result = speaker.speak(make_reminder())
    assert result.attempted is False
    assert result.spoken is False
    assert result.media_url == "http://host:8090/head_down_1.mp3"


def test_command_template_substitution():
    speaker = Speaker(
        settings(
            speak_cmd="/bin/echo {class} {variant} {url}",
            media_base_url="http://host:8090",
        )
    )
    result = speaker.speak(make_reminder())
    assert result.attempted is True
    assert result.spoken is True
    assert result.media_url == "http://host:8090/head_down_1.mp3"


def test_bad_template_is_reported_not_raised():
    speaker = Speaker(settings(speak_cmd="/bin/echo {missing}"))
    result = speaker.speak(make_reminder())
    assert result.attempted is False
    assert result.spoken is False
