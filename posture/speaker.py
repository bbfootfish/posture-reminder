"""Speech output through the configured ``SPEAK_CMD``.

The command template may use ``{class}``, ``{variant}``, ``{url}`` and
``{text}``.  With no command configured the service runs in dry-run mode and
only logs the line it would have spoken.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass

from .config import Settings
from .models import Reminder
from .phrases import PHRASES, phrase_url

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeakResult:
    attempted: bool
    spoken: bool
    media_url: str
    detail: str = ""


class Speaker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def media_url(self, reminder: Reminder) -> str:
        phrase = PHRASES[reminder.issue][reminder.variant]
        return phrase_url(self.settings.media_base_url, phrase)

    def speak(self, reminder: Reminder) -> SpeakResult:
        url = self.media_url(reminder)
        if not self.settings.speak_cmd.strip():
            LOGGER.info(
                "dry-run (SPEAK_CMD unset): class=%s variant=%s text=%s url=%s",
                reminder.issue,
                reminder.variant,
                reminder.text,
                url,
            )
            return SpeakResult(attempted=False, spoken=False, media_url=url, detail="dry-run")

        values = {
            "class": reminder.issue,
            "variant": reminder.variant,
            "url": url,
            "text": reminder.text,
        }
        try:
            command = self.settings.speak_cmd.format(**values)
            argv = shlex.split(command)
        except (KeyError, ValueError) as exc:
            LOGGER.error("invalid SPEAK_CMD template: %s", exc)
            return SpeakResult(
                attempted=False,
                spoken=False,
                media_url=url,
                detail=f"invalid SPEAK_CMD: {exc}",
            )
        if not argv:
            LOGGER.error("SPEAK_CMD expanded to an empty command")
            return SpeakResult(
                attempted=False,
                spoken=False,
                media_url=url,
                detail="empty SPEAK_CMD",
            )

        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.settings.speak_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOGGER.error("speak command failed: %s", exc)
            return SpeakResult(attempted=True, spoken=False, media_url=url, detail=str(exc))

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:500]
            LOGGER.error("speak command exited %s: %s", completed.returncode, detail)
            return SpeakResult(attempted=True, spoken=False, media_url=url, detail=detail)

        LOGGER.info(
            "spoke class=%s variant=%s text=%s url=%s",
            reminder.issue,
            reminder.variant,
            reminder.text,
            url,
        )
        return SpeakResult(attempted=True, spoken=True, media_url=url)
