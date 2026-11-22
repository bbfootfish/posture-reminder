"""Replay a metric CSV through the real state machine.

Example::

    python -m posture.replay --csv sample_metrics.csv

CSV columns: ``t,s_norm,head_up,head_fwd``.  ``t`` is seconds from the start
time.  A row with all three metrics empty represents "no pose" and is fed to
the state machine as such.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .baseline import BaselineError, BaselineStore
from .config import Settings
from .schedule import Mode
from .models import Baseline, Metrics
from .state_machine import ReminderStateMachine

# The ticket's measured "normal writing" baseline.  Replay is self-contained
# by default; pass --baseline-json to use the real calibrated file.
DEFAULT_BASELINE = Baseline(s_norm=0.0875, head_up=0.38, head_fwd=164.0)


def _load_baseline(path: str | None) -> Baseline:
    if not path:
        return DEFAULT_BASELINE
    store = BaselineStore(Path(path))
    baseline = store.load()
    if baseline is None:
        raise BaselineError(f"baseline file not found: {path}")
    return baseline


def _parse_start(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid --start value {value!r}") from exc


def _parse_row(row: dict[str, str], line_number: int) -> tuple[float, Metrics | None]:
    raw_t = (row.get("t") or "").strip()
    if not raw_t:
        raise ValueError(f"line {line_number}: t is required")
    try:
        seconds = float(raw_t)
    except ValueError as exc:
        raise ValueError(f"line {line_number}: invalid t={raw_t!r}") from exc

    values = [(row.get(key) or "").strip() for key in ("s_norm", "head_up", "head_fwd")]
    if not any(values):
        return seconds, None
    if not all(values):
        raise ValueError(
            f"line {line_number}: s_norm/head_up/head_fwd must be all set or all empty"
        )
    try:
        metrics = Metrics(
            s_norm=float(values[0]),
            head_up=float(values[1]),
            head_fwd=float(values[2]),
        )
    except ValueError as exc:
        raise ValueError(f"line {line_number}: invalid metric value") from exc
    return seconds, metrics


def load_rows(path: Path) -> list[tuple[float, Metrics | None]]:
    rows: list[tuple[float, Metrics | None]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV is empty")
        missing = {"t", "s_norm", "head_up", "head_fwd"} - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        for index, row in enumerate(reader, start=2):
            rows.append(_parse_row(row, index))
    return rows


def run(
    rows: Iterable[tuple[float, Metrics | None]],
    *,
    baseline: Baseline,
    settings: Settings,
    start: datetime,
    ignore_schedule: bool = False,
) -> list[Any]:
    machine = ReminderStateMachine(baseline, settings)
    schedule = settings.schedule
    reminders = []
    previous = -1.0
    for seconds, metrics in rows:
        if seconds < previous:
            raise ValueError("CSV timestamps must be non-decreasing")
        previous = seconds
        at = start + timedelta(seconds=seconds)
        mode = Mode.ALLOW if ignore_schedule else schedule.mode(at)
        if mode is Mode.BLOCK:
            # Blocked rows are skipped exactly as the live loop skips them.
            continue
        reminder = machine.observe(metrics, at, mode=mode)
        if reminder is not None:
            reminders.append(reminder)
    return reminders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="metric CSV to replay")
    parser.add_argument(
        "--baseline-json",
        default=None,
        help="baseline.json from calibration (default: ticket's measured baseline)",
    )
    parser.add_argument(
        "--start",
        default="2026-11-13T16:00:00",
        help="local start datetime (default: 2026-11-13T16:00:00)",
    )
    parser.add_argument(
        "--ignore-schedule",
        action="store_true",
        help="judge every row regardless of time-of-day blocks (offline tuning)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)

    try:
        rows = load_rows(Path(args.csv))
        baseline = _load_baseline(args.baseline_json)
        start = _parse_start(args.start)
        settings = Settings.from_env()
        reminders = run(
            rows,
            baseline=baseline,
            settings=settings,
            start=start,
            ignore_schedule=args.ignore_schedule,
        )
    except (OSError, ValueError, BaselineError) as exc:
        print(f"replay error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "samples": len(rows),
                    "baseline": baseline.as_dict(),
                    "start": start.isoformat(timespec="seconds"),
                    "reminders": [reminder.as_dict() for reminder in reminders],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        "baseline: s_norm={s_norm:.4f} head_up={head_up:.3f} head_fwd={head_fwd:.1f}".format(
            **baseline.as_dict()
        )
    )
    print(f"samples: {len(rows)}  start: {start.isoformat(timespec='seconds')}")
    if not reminders:
        print("no reminders")
    for index, reminder in enumerate(reminders, start=1):
        metrics = reminder.metrics
        print(
            f"[{reminder.at.strftime('%H:%M:%S')}] #{index} {reminder.issue} "
            f"variant={reminder.variant} text={reminder.text} "
            f"(s_norm={metrics.s_norm:.4f} head_up={metrics.head_up:.3f} "
            f"head_fwd={metrics.head_fwd:.1f})"
        )
    by_issue: dict[str, int] = {}
    for reminder in reminders:
        by_issue[reminder.issue] = by_issue.get(reminder.issue, 0) + 1
    print(f"reminders: {len(reminders)}  by_class: {by_issue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
