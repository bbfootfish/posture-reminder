"""SQLite reminder log.

Only the timestamp, class, metric snapshot and an optional *filename* of a
local alert image are stored; image bytes never enter the database.

Each reminder can also carry a human verdict (true_positive / false_positive)
recorded from the dashboard, used to advise threshold tuning.  Verdicts are
nullable, editable and never affect live judgement directly.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Reminder

# Stable verdict values shared by the API, storage and dashboard.
VERDICT_TRUE = "true_positive"
VERDICT_FALSE = "false_positive"
# An event that is not a real judgement at all (e.g. a false pose detected on
# an empty chair).  Invalid events are excluded from tuning statistics: they
# are data-quality problems, not evidence about thresholds.
VERDICT_INVALID = "invalid"
VERDICTS = (VERDICT_TRUE, VERDICT_FALSE, VERDICT_INVALID)
# Only these two verdicts describe a real decision and feed tuning advice.
TUNABLE_VERDICTS = (VERDICT_TRUE, VERDICT_FALSE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    issue TEXT NOT NULL,
    s_norm REAL NOT NULL,
    head_up REAL NOT NULL,
    head_fwd REAL NOT NULL,
    image TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_at ON reminders(at);
"""

# A human-marked missed event: a moment the parent believes should have
# triggered a reminder but did not.  Distinct from reminders (which only
# contain firings).  Metrics are a snapshot taken from the nearest sample at
# mark time so tuning advice can reason about the missed moment.
_MISSED_SCHEMA = """
CREATE TABLE IF NOT EXISTS missed_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    ts REAL NOT NULL,
    issue TEXT NOT NULL,
    note TEXT,
    head_up REAL,
    head_fwd REAL,
    s_norm REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_missed_ts ON missed_events(ts);
CREATE INDEX IF NOT EXISTS idx_missed_issue ON missed_events(issue);
"""


class MissedEventStore:
    """CRUD for human-marked missed reminders.

    Edits and deletes are allowed so a parent can correct a mis-marked moment.
    All reads are newest-first.  The store never affects live judgement.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        with self._connection:
            self._connection.executescript(_MISSED_SCHEMA)

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, object]:
        return {
            "id": row[0],
            "at": row[1],
            "ts": row[2],
            "issue": row[3],
            "note": row[4],
            "head_up": row[5],
            "head_fwd": row[6],
            "s_norm": row[7],
            "created_at": row[8],
        }

    def add(
        self,
        at: datetime,
        issue: str,
        note: str | None,
        head_up: float | None = None,
        head_fwd: float | None = None,
        s_norm: float | None = None,
    ) -> dict[str, object]:
        with self._lock:
            created = datetime.now().isoformat(timespec="seconds")
            with self._connection:
                cursor = self._connection.execute(
                    """INSERT INTO missed_events
                       (at, ts, issue, note, head_up, head_fwd, s_norm, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        at.isoformat(timespec="seconds"),
                        at.timestamp(),
                        issue,
                        (note or "").strip() or None,
                        head_up,
                        head_fwd,
                        s_norm,
                        created,
                    ),
                )
                new_id = int(cursor.lastrowid)
            row = self._connection.execute(
                "SELECT id, at, ts, issue, note, head_up, head_fwd, s_norm, created_at "
                "FROM missed_events WHERE id = ?",
                (new_id,),
            ).fetchone()
            return self._row_to_dict(row)

    def update(
        self,
        event_id: int,
        *,
        issue: str | None = None,
        note: str | None = None,
    ) -> dict[str, object] | None:
        """Update issue and/or note.  ``note=None`` clears the note."""
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM missed_events WHERE id = ?", (int(event_id),)
            ).fetchone()
            if row is None:
                return None
            sets: list[str] = []
            params: list[Any] = []
            if issue is not None:
                sets.append("issue = ?")
                params.append(issue)
            if note is not None:
                sets.append("note = ?")
                params.append((note or "").strip() or None)
            if sets:
                params.append(int(event_id))
                with self._connection:
                    self._connection.execute(
                        "UPDATE missed_events SET " + ", ".join(sets) + " WHERE id = ?",
                        params,
                    )
            updated = self._connection.execute(
                "SELECT id, at, ts, issue, note, head_up, head_fwd, s_norm, created_at "
                "FROM missed_events WHERE id = ?",
                (int(event_id),),
            ).fetchone()
            return self._row_to_dict(updated)

    def delete(self, event_id: int) -> bool:
        with self._lock:
            with self._connection:
                cursor = self._connection.execute(
                    "DELETE FROM missed_events WHERE id = ?", (int(event_id),)
                )
            return cursor.rowcount > 0

    def list(self, since: datetime | None = None, limit: int = 200) -> list[dict[str, object]]:
        with self._lock:
            if since is not None:
                rows = self._connection.execute(
                    """SELECT id, at, ts, issue, note, head_up, head_fwd, s_norm, created_at
                       FROM missed_events WHERE ts >= ? ORDER BY ts DESC LIMIT ?""",
                    (since.timestamp(), max(1, int(limit))),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT id, at, ts, issue, note, head_up, head_fwd, s_norm, created_at
                       FROM missed_events ORDER BY ts DESC LIMIT ?""",
                    (max(1, int(limit)),),
                ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def stats(self) -> list[dict[str, object]]:
        """Aggregate missed events by issue for tuning advice."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT issue, COUNT(*),
                       AVG(head_up), MIN(head_up), MAX(head_up),
                       AVG(head_fwd), MIN(head_fwd), MAX(head_fwd),
                       AVG(s_norm), MIN(s_norm), MAX(s_norm)
                FROM missed_events
                GROUP BY issue
                ORDER BY issue
                """
            ).fetchall()
        return [
            {
                "issue": r[0],
                "count": int(r[1]),
                "head_up": {"avg": r[2], "min": r[3], "max": r[4]},
                "head_fwd": {"avg": r[5], "min": r[6], "max": r[7]},
                "s_norm": {"avg": r[8], "min": r[9], "max": r[10]},
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class ReminderStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        with self._connection:
            self._connection.executescript(_SCHEMA)
            columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(reminders)")
            }
            if "image" not in columns:
                self._connection.execute("ALTER TABLE reminders ADD COLUMN image TEXT")
            # Human-verdict columns are added idempotently so an existing DB is
            # upgraded in place on first start, matching the "image" migration.
            for column, decl in (
                ("verdict", "TEXT"),
                ("verdict_at", "TEXT"),
                ("verdict_note", "TEXT"),
            ):
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE reminders ADD COLUMN {column} {decl}"
                    )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_verdict "
                "ON reminders(verdict)"
            )

    def record(self, reminder: Reminder) -> None:
        """Store the timestamp, class, metric snapshot and image filename."""
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO reminders (at, issue, s_norm, head_up, head_fwd, image)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder.at.isoformat(timespec="seconds"),
                    reminder.issue,
                    reminder.metrics.s_norm,
                    reminder.metrics.head_up,
                    reminder.metrics.head_fwd,
                    reminder.image,
                ),
            )

    def count_since(self, start: datetime) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM reminders WHERE at >= ?",
                (start.isoformat(timespec="seconds"),),
            ).fetchone()
        return int(row[0]) if row else 0

    def recent_since(self, start: datetime, limit: int = 50) -> list[dict[str, object]]:
        """Return recent reminders (newest first) for the dashboard."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, at, issue, s_norm, head_up, head_fwd, image,
                       verdict, verdict_at, verdict_note
                FROM reminders
                WHERE at >= ?
                ORDER BY at DESC
                LIMIT ?
                """,
                (start.isoformat(timespec="seconds"), max(1, int(limit))),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, object]:
        return {
            "id": row[0],
            "at": row[1],
            "issue": row[2],
            "s_norm": row[3],
            "head_up": row[4],
            "head_fwd": row[5],
            "image": row[6],
            "verdict": row[7],
            "verdict_at": row[8],
            "verdict_note": row[9],
        }

    def set_verdict(
        self,
        reminder_id: int,
        verdict: str | None,
        note: str | None = None,
    ) -> dict[str, object] | None:
        """Set, change or clear a human verdict on one reminder.

        ``verdict`` must be one of :data:`VERDICTS` or ``None`` (clear).
        Returns the updated row, or ``None`` when the id does not exist.
        """
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(f"invalid verdict: {verdict!r}")
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM reminders WHERE id = ?", (int(reminder_id),)
            ).fetchone()
            if row is None:
                return None
            now = datetime.now().isoformat(timespec="seconds")
            verdict_at = now if verdict is not None else None
            if verdict is not None:
                note_value = (note or "").strip() or None
            else:
                note_value = None
            with self._connection:
                self._connection.execute(
                    """UPDATE reminders
                       SET verdict = ?, verdict_at = ?, verdict_note = ?
                       WHERE id = ?""",
                    (verdict, verdict_at, note_value, int(reminder_id)),
                )
            updated = self._connection.execute(
                """SELECT id, at, issue, s_norm, head_up, head_fwd, image,
                          verdict, verdict_at, verdict_note
                   FROM reminders WHERE id = ?""",
                (int(reminder_id),),
            ).fetchone()
            return self._row_to_dict(updated) if updated else None

    def verdict_stats(self) -> list[dict[str, object]]:
        """Aggregate human-labelled reminders by issue for tuning advice.

        Only true/false positives are counted.  Unlabelled and 'invalid' rows
        are excluded so recommendations reflect real judgements, not junk
        events such as empty-chair false poses.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT issue, verdict, COUNT(*),
                       AVG(head_up), MIN(head_up), MAX(head_up),
                       AVG(head_fwd), MIN(head_fwd), MAX(head_fwd),
                       AVG(s_norm), MIN(s_norm), MAX(s_norm)
                FROM reminders
                WHERE verdict IN (?, ?)
                GROUP BY issue, verdict
                ORDER BY issue, verdict
                """,
                TUNABLE_VERDICTS,
            ).fetchall()
        return [
            {
                "issue": r[0],
                "verdict": r[1],
                "count": int(r[2]),
                "head_up": {"avg": r[3], "min": r[4], "max": r[5]},
                "head_fwd": {"avg": r[6], "min": r[7], "max": r[8]},
                "s_norm": {"avg": r[9], "min": r[10], "max": r[11]},
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
