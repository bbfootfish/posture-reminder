"""Three-state day schedule: hard blocks, the late-night soft block, allowance.

The service used to gate on a single "allowed window" (``WORK_HOURS``), which
cannot express two independent rules at once.  Here a moment resolves to one of
three modes:

``BLOCK``          no inference, no samples, no judging, no speech
``MONITOR_ONLY``   inference and samples, but no speech and no alert images
``ALLOW``          everything

The rules, in order: night ``00:00-08:00`` (every day), school hours
``08:00-18:00`` (school days only), then late night ``22:00-24:00`` (every day).

School days are decided by ``chinese_calendar``, which already encodes the
State Council's holiday swaps: a make-up Saturday counts as a school day and a
weekday inside National Day does not.  That library only covers 2004-2027; past
its range this module fails closed (treats the day as a school day, so the
daytime block stays on) and warns once per calendar day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any

LOGGER = logging.getLogger(__name__)

SUPPORTED_YEARS: tuple[int, int] = (2004, 2027)


class Mode(str, Enum):
    """What the service is allowed to do at a given moment."""

    BLOCK = "block"
    MONITOR_ONLY = "monitor_only"
    ALLOW = "allow"


def _parse_hhmm(value: str) -> time:
    raw = (value or "").strip()
    if raw.startswith("24:00"):
        # "24:00" is the end of the day, not a valid clock time.  Callers write
        # 22:00-24:00 to mean "until midnight", so map it to time.max and let
        # BlockWindow.contains treat that end as inclusive.
        return time.max
    try:
        hour, minute = raw.split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid HH:MM value: {raw!r}") from exc


def _format_hhmm(value: time) -> str:
    if value == time.max:
        return "24:00"
    return value.strftime("%H:%M")


@dataclass(frozen=True)
class BlockWindow:
    """A local-time window that is blocked.  ``start`` inclusive, ``end`` exclusive.

    ``end`` of :data:`datetime.time.max` (written ``24:00``) is treated as
    inclusive, because the last representable microsecond of a day is
    ``23:59:59.999999`` — an exclusive upper bound at midnight would leave that
    microsecond unclassified.
    """

    start: time
    end: time

    @classmethod
    def parse(cls, text: str) -> BlockWindow:
        raw = (text or "").strip()
        if "-" not in raw:
            raise ValueError(f"window must look like 08:00-18:00, got {text!r}")
        left, right = raw.split("-", 1)
        return cls(start=_parse_hhmm(left), end=_parse_hhmm(right))

    def contains(self, at: datetime) -> bool:
        current = at.time()
        if self.start <= self.end:
            if self.end == time.max:
                return self.start <= current
            return self.start <= current < self.end
        return current >= self.start or current < self.end

    def __str__(self) -> str:
        return f"{_format_hhmm(self.start)}-{_format_hhmm(self.end)}"


@dataclass(frozen=True)
class DateRange:
    """An inclusive date range, used for school breaks."""

    start: date
    end: date
    label: str = ""

    @classmethod
    def parse(cls, start: str, end: str, label: str = "") -> DateRange:
        try:
            first = date.fromisoformat(start)
            last = date.fromisoformat(end)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid date range {start!r}..{end!r}") from exc
        if last < first:
            raise ValueError(f"date range ends before it starts: {start}..{end}")
        return cls(start=first, end=last, label=label)

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass
class SchoolCalendar:
    """Decides whether a date is a school day.

    ``breaks`` covers winter/summer holidays, which the holiday library knows
    nothing about.  A day inside a break is not a school day, so the daytime
    block lifts and the service resumes monitoring.
    """

    breaks: tuple[DateRange, ...] = ()
    supported_years: tuple[int, int] = SUPPORTED_YEARS
    _warned_on: date | None = field(default=None, repr=False, compare=False)

    # -- holiday library access, with the out-of-range guard ---------------
    def _out_of_range(self, day: date) -> bool:
        low, high = self.supported_years
        return not (low <= day.year <= high)

    def _warn_once(self, day: date) -> None:
        if self._warned_on == day:
            return
        self._warned_on = day
        low, high = self.supported_years
        LOGGER.warning(
            "calendar data unavailable for %s (supported %s-%s); "
            "treating it as a school day so the daytime block stays on. "
            "Upgrade the chinese-calendar package to fix this.",
            day,
            low,
            high,
        )

    def calendar_ok(self, day: date) -> bool:
        return not self._out_of_range(day)

    def _is_statutory_workday(self, day: date) -> bool:
        import chinese_calendar as calendar_module

        return bool(calendar_module.is_workday(day))

    def _holiday_detail(self, day: date) -> tuple[bool, str | None]:
        import chinese_calendar as calendar_module

        return calendar_module.get_holiday_detail(day)

    # -- public API -------------------------------------------------------
    def break_label(self, day: date) -> str | None:
        for entry in self.breaks:
            if entry.contains(day):
                return entry.label or "假期"
        return None

    def holiday_name(self, day: date) -> str | None:
        if self._out_of_range(day):
            self._warn_once(day)
            return None
        try:
            is_holiday, name = self._holiday_detail(day)
        except Exception:  # pragma: no cover - library quirks
            self._warn_once(day)
            return None
        return name if is_holiday else None

    def is_school_day(self, day: date) -> bool:
        if self.break_label(day) is not None:
            return False
        if self._out_of_range(day):
            self._warn_once(day)
            return True
        try:
            is_holiday, _ = self._holiday_detail(day)
            if is_holiday:
                return False
            return self._is_statutory_workday(day)
        except Exception:  # pragma: no cover - library quirks
            self._warn_once(day)
            return True


@dataclass(frozen=True)
class ServiceSchedule:
    """Resolve a local datetime to a :class:`Mode`."""

    night: BlockWindow
    school: BlockWindow
    late_night: BlockWindow
    calendar: SchoolCalendar

    @classmethod
    def defaults(cls, *, breaks: tuple[DateRange, ...] = ()) -> ServiceSchedule:
        return cls(
            night=BlockWindow.parse("00:00-08:00"),
            school=BlockWindow.parse("08:00-18:00"),
            late_night=BlockWindow.parse("22:00-24:00"),
            calendar=SchoolCalendar(breaks=breaks),
        )

    def mode(self, at: datetime) -> Mode:
        if self.night.contains(at):
            return Mode.BLOCK
        if self.calendar.is_school_day(at.date()) and self.school.contains(at):
            return Mode.BLOCK
        if self.late_night.contains(at):
            return Mode.MONITOR_ONLY
        return Mode.ALLOW

    # Thin proxies so callers (and tests) do not have to know that school-day
    # logic lives one level down in SchoolCalendar.
    def is_school_day(self, day: date) -> bool:
        return self.calendar.is_school_day(day)

    def calendar_ok(self, day: date) -> bool:
        return self.calendar.calendar_ok(day)

    def _reason(self, at: datetime) -> str:
        if self.night.contains(at):
            return "night"
        if self.school.contains(at):
            if self.calendar.break_label(at.date()):
                return "school_break"
            if not self.calendar.is_school_day(at.date()):
                if self.calendar.holiday_name(at.date()):
                    return "holiday"
                return "weekend"
            return "school_hours"
        if self.late_night.contains(at):
            return "late_night"
        return "allowed"

    def explain(self, at: datetime) -> dict[str, Any]:
        """Everything the API and dashboard need, computed in one place."""
        mode = self.mode(at)
        day = at.date()
        school_day = self.calendar.is_school_day(day)
        return {
            "mode": mode.value,
            "reason": self._reason(at),
            "next_change_at": self.next_change_at(at).isoformat(timespec="seconds"),
            "next_allow_at": self.next_allowed_at(at).isoformat(timespec="seconds"),
            "school_day": school_day,
            "holiday_name": self.calendar.holiday_name(day),
            "break_label": self.calendar.break_label(day),
            "calendar_ok": self.calendar.calendar_ok(day),
            "calendar_supported_years": list(self.calendar.supported_years),
        }

    def next_change_at(self, at: datetime) -> datetime:
        """First moment the mode differs from ``mode(at)``.

        Only the window boundaries and midnight can change the mode, so probing
        those is exact and cheap — this runs on every /healthz poll (2 s), and a
        minute-by-minute walk would do 2880 iterations each time.
        """
        current = self.mode(at)
        candidates: list[datetime] = []
        for offset in (0, 1):
            day = (at + timedelta(days=offset)).date()
            for moment in (
                datetime.combine(day, datetime.min.time()),
                datetime.combine(day, self.night.start),
                datetime.combine(day, self.night.end),
                datetime.combine(day, self.school.start),
                datetime.combine(day, self.school.end),
                datetime.combine(day, self.late_night.start),
                datetime.combine(day, self.late_night.end),
            ):
                if moment > at and self.mode(moment) is not current:
                    candidates.append(moment)
        if not candidates:
            # No boundary within two days (only possible with custom windows);
            # fall back to a coarse walk so the value stays truthful.
            probe = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
            for _ in range(3 * 24 * 60 + 1):
                if self.mode(probe) is not current:
                    return probe
                probe += timedelta(minutes=1)
            return probe
        return min(candidates)

    def next_allowed_at(self, at: datetime) -> datetime:
        """Next moment the service runs with speech enabled.

        Uses the same boundary set as :meth:`next_change_at`, so it costs a
        handful of comparisons instead of a 4320-step minute walk.  Repeatedly
        stepping to the next boundary is at most a few days of hops.

        Verified against the real calendar: Fri 10:00 → Fri 18:00,
        Sat 03:00 → Sat 08:00, Fri 22:30 → Sat 08:00, Sun 23:00 → Mon 18:00.
        """
        probe = at
        for _ in range(96):  # 96 boundaries is far more than three days' worth
            if self.mode(probe) is Mode.ALLOW:
                return probe
            following = self.next_change_at(probe)
            if following <= probe:
                # Guard: never spin if a boundary lands on itself.
                return probe
            probe = following
        return probe

    def summary(self) -> str:
        return (
            f"上学日 {self.night} 与 {self.school} 不检测；"
            f"每日 {self.late_night} 只记录不播报"
        )