"""Small helper for "a person has been here long enough" detection."""

from __future__ import annotations

from datetime import datetime


class PresenceTimer:
    """Tracks continuous presence with a short grace period.

    A single missed pose frame is common (fidgeting, a hand over the face), so
    absence shorter than ``grace_seconds`` does not restart the timer.  Once
    the requested duration is reached, ``update`` returns ``True`` exactly
    once until the timer is reset or presence is lost for longer than the
    grace period.
    """

    def __init__(self, seconds: float, grace_seconds: float = 1.0) -> None:
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        self.seconds = seconds
        self.grace_seconds = max(0.0, grace_seconds)
        self._since: datetime | None = None
        self._last_present_at: datetime | None = None
        self._fired = False

    def update(self, present: bool, at: datetime) -> bool:
        if present:
            self._last_present_at = at
            if self._since is None:
                self._since = at
            if not self._fired and (at - self._since).total_seconds() >= self.seconds:
                self._fired = True
                return True
            return False

        if self._last_present_at is None:
            self._since = None
            return False
        if (at - self._last_present_at).total_seconds() > self.grace_seconds:
            self.reset()
        return False

    def reset(self) -> None:
        self._since = None
        self._last_present_at = None
        self._fired = False
