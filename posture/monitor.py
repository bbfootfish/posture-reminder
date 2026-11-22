"""Persistent monitoring history for the dashboard.

Only the three side-view metrics, the issue class and the decision state are
stored: no frames, landmarks or identity.  Samples are throttled to at most
one per second and kept for 25 hours, then pruned.  The same SQLite file as
the reminder log is used, in WAL mode, so the 15-minute live chart and the
24-hour review survive service restarts.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    ts REAL NOT NULL,
    s_norm REAL,
    head_up REAL,
    head_fwd REAL,
    issue TEXT,
    state TEXT NOT NULL,
    occupancy REAL NOT NULL DEFAULT 0,
    person INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
"""


@dataclass(frozen=True)
class MonitorPoint:
    """One judgement sample, already smoothed, for the timeline charts."""

    at: datetime
    s_norm: float | None
    head_up: float | None
    head_fwd: float | None
    issue: str | None
    state: str
    occupancy: float
    person: bool

    @classmethod
    def from_snapshot(
        cls,
        at: datetime,
        snapshot: dict[str, Any],
        *,
        person: bool,
    ) -> MonitorPoint:
        smoothed = snapshot.get("smoothed")
        values = smoothed if isinstance(smoothed, dict) else {}
        window = snapshot.get("window")
        occupancy = 0.0
        if isinstance(window, dict):
            occupancy = float(window.get("occupancy") or 0.0)
        return cls(
            at=at,
            s_norm=_optional_float(values.get("s_norm")),
            head_up=_optional_float(values.get("head_up")),
            head_fwd=_optional_float(values.get("head_fwd")),
            issue=snapshot.get("issue") if isinstance(snapshot.get("issue"), str) else None,
            state=str(snapshot.get("status") or "starting"),
            occupancy=occupancy,
            person=person,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "t": self.at.isoformat(timespec="milliseconds"),
            "s": None if self.s_norm is None else round(self.s_norm, 5),
            "hu": None if self.head_up is None else round(self.head_up, 4),
            "hf": None if self.head_fwd is None else round(self.head_fwd, 2),
            "occ": round(self.occupancy, 3),
            "issue": self.issue,
            "state": self.state,
            "person": self.person,
        }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class MonitorHistory:
    """SQLite-backed history with a bounded, throttled write rate."""

    def __init__(
        self,
        path: Path,
        *,
        retention_hours: float = 25.0,
        interval_seconds: float = 1.0,
        prune_every: int = 600,
    ) -> None:
        if retention_hours <= 0:
            raise ValueError("retention_hours must be > 0")
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_hours = retention_hours
        self.interval_seconds = interval_seconds
        self.prune_every = max(1, int(prune_every))
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        with self._connection:
            self._connection.executescript(_SCHEMA)
        self._last_at: datetime | None = None
        self._since_prune = 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def maybe_add(
        self,
        at: datetime,
        snapshot: dict[str, Any],
        *,
        person: bool,
    ) -> MonitorPoint | None:
        """Record a point unless the interval has not elapsed yet."""
        with self._lock:
            if self._last_at is not None:
                elapsed = (at - self._last_at).total_seconds()
                if elapsed < self.interval_seconds:
                    return None
            point = MonitorPoint.from_snapshot(at, snapshot, person=person)
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO samples
                        (at, ts, s_norm, head_up, head_fwd, issue, state, occupancy, person)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        point.at.isoformat(timespec="milliseconds"),
                        point.at.timestamp(),
                        point.s_norm,
                        point.head_up,
                        point.head_fwd,
                        point.issue,
                        point.state,
                        point.occupancy,
                        1 if point.person else 0,
                    ),
                )
            self._last_at = at
            self._since_prune += 1
            if self._since_prune >= self.prune_every:
                self._since_prune = 0
                self._prune(at)
            return point

    def series(
        self,
        *,
        now: datetime,
        minutes: float,
        since: datetime | None = None,
        max_points: int = 600,
    ) -> list[dict[str, Any]]:
        """Return recent points, thinned for the live chart.

        Polling clients pass the timestamp of their last point, so the normal
        response is only a handful of new samples.  A full request is thinned
        to at most ``max_points`` so the dashboard payload stays small.
        """
        with self._lock:
            if since is not None:
                rows = self._connection.execute(
                    """
                    SELECT at, s_norm, head_up, head_fwd, issue, state, occupancy, person
                    FROM samples WHERE ts > ? ORDER BY ts
                    """,
                    (since.timestamp(),),
                ).fetchall()
            else:
                cutoff = (now - timedelta(minutes=max(0.0, minutes))).timestamp()
                rows = self._connection.execute(
                    """
                    SELECT at, s_norm, head_up, head_fwd, issue, state, occupancy, person
                    FROM samples WHERE ts >= ? ORDER BY ts
                    """,
                    (cutoff,),
                ).fetchall()
        points = [
            {
                "t": row[0],
                "s": None if row[1] is None else round(row[1], 5),
                "hu": None if row[2] is None else round(row[2], 4),
                "hf": None if row[3] is None else round(row[3], 2),
                "issue": row[4],
                "state": row[5],
                "occ": round(row[6], 3),
                "person": bool(row[7]),
            }
            for row in rows
        ]
        return _thin(points, max_points)

    def buckets(
        self,
        *,
        now: datetime,
        hours: float,
        bucket_seconds: float,
    ) -> list[dict[str, Any]]:
        """Aggregate samples into fixed buckets for the 24-hour review.

        Each bucket carries the average metrics and the fraction of samples
        that were out of line and had a person present, so the page can show
        both the measurement and the resulting score without shipping tens of
        thousands of raw rows.
        """
        bucket = max(1, int(bucket_seconds))
        cutoff = (now - timedelta(hours=max(0.1, hours))).timestamp()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT CAST(ts / ? AS INTEGER) AS bucket,
                       COUNT(*),
                       SUM(CASE WHEN issue IS NOT NULL THEN 1 ELSE 0 END),
                       SUM(CASE WHEN person = 1 THEN 1 ELSE 0 END),
                       AVG(s_norm), AVG(head_up), AVG(head_fwd),
                       AVG(occupancy), MAX(occupancy)
                FROM samples
                WHERE ts >= ?
                GROUP BY bucket
                ORDER BY bucket
                """,
                (bucket, cutoff),
            ).fetchall()
        return [_bucket_row(row, bucket) for row in rows]

    def range_buckets(
        self,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: float,
    ) -> list[dict[str, Any]]:
        """Aggregate an explicit ``[start, end]`` window into buckets.

        Unlike :meth:`buckets`, the window is anchored by the caller rather
        than "the last N hours", and ``bucket_seconds`` may be as small as 1,
        so a zoomed-in timeline can show individual seconds.  Only buckets that
        contain samples are returned; empty gaps are simply absent.
        """
        left, right = (start, end) if start <= end else (end, start)
        bucket = max(1, int(bucket_seconds))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT CAST(ts / ? AS INTEGER) AS bucket,
                       COUNT(*),
                       SUM(CASE WHEN issue IS NOT NULL THEN 1 ELSE 0 END),
                       SUM(CASE WHEN person = 1 THEN 1 ELSE 0 END),
                       AVG(s_norm), AVG(head_up), AVG(head_fwd),
                       AVG(occupancy), MAX(occupancy)
                FROM samples
                WHERE ts >= ? AND ts <= ?
                GROUP BY bucket
                ORDER BY bucket
                """,
                (bucket, left.timestamp(), right.timestamp()),
            ).fetchall()
        return [_bucket_row(row, bucket) for row in rows]

    def nearest(
        self, at: datetime, tolerance_seconds: float = 5.0
    ) -> dict[str, float | None] | None:
        """Return the metric snapshot of the sample nearest to ``at``.

        Used when a human marks a missed event at a remembered time: the
        recorded metrics come from real samples within a small tolerance, so
        tuning advice is grounded in what the detector actually saw.  Returns
        ``None`` when no sample is close enough (e.g. retention expired).
        """
        target = at.timestamp()
        low, high = target - tolerance_seconds, target + tolerance_seconds
        with self._lock:
            row = self._connection.execute(
                """
                SELECT head_up, head_fwd, s_norm, ts
                FROM samples
                WHERE ts >= ? AND ts <= ?
                ORDER BY ABS(ts - ?) ASC
                LIMIT 1
                """,
                (low, high, target),
            ).fetchone()
        if row is None:
            return None
        return {
            "head_up": _optional_float(row[0]),
            "head_fwd": _optional_float(row[1]),
            "s_norm": _optional_float(row[2]),
            "ts": float(row[3]),
        }

    def _prune(self, at: datetime) -> None:
        cutoff = at.timestamp() - self.retention_hours * 3600.0
        with self._connection:
            self._connection.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))


def _bucket_row(row: tuple[Any, ...], bucket: int) -> dict[str, Any]:
    bucket_id, samples, outliers, present, s_norm, head_up, head_fwd, occupancy, occupancy_max = row
    start = datetime.fromtimestamp(bucket_id * bucket)
    return {
        "t": start.isoformat(timespec="seconds"),
        "samples": int(samples),
        "outliers": int(outliers),
        "outlier_ratio": round(int(outliers) / int(samples), 3) if samples else 0.0,
        "person": int(present),
        "person_ratio": round(int(present) / int(samples), 3) if samples else 0.0,
        "s_norm": None if s_norm is None else round(float(s_norm), 5),
        "head_up": None if head_up is None else round(float(head_up), 4),
        "head_fwd": None if head_fwd is None else round(float(head_fwd), 2),
        "occupancy": None if occupancy is None else round(float(occupancy), 3),
        "occupancy_max": None if occupancy_max is None else round(float(occupancy_max), 3),
    }


def _thin(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if max_points <= 0 or not points:
        return []
    if len(points) <= max_points:
        return points
    stride = max(1, math.ceil(len(points) / max_points))
    sampled = points[::stride]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled
