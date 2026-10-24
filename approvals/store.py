"""
SQLite store for pending actions.

SQLite rather than Postgres: this is a single-user personal tool, the data is
tiny, and a file is one fewer thing to run. It is also the audit log -- every
proposal, every answer, every applied change, kept permanently, because
"what did it do to my lineup and why" must be answerable after the fact.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from approvals.models import ActionKind, ActionStatus, PendingAction

DEFAULT_PATH = Path(".cache/benchcoach.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    summary      TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    responded_at TEXT,
    applied_at   TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS ix_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS ix_actions_code   ON actions(code);
"""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    # Rows written before tz-awareness was enforced would otherwise compare
    # against aware datetimes and raise.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


class ActionStore:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> PendingAction:
        return PendingAction(
            action_id=row["id"],
            code=row["code"],
            kind=ActionKind(row["kind"]),
            summary=row["summary"],
            reason=row["reason"],
            payload=json.loads(row["payload"]),
            status=ActionStatus(row["status"]),
            created_at=_parse(row["created_at"]),
            expires_at=_parse(row["expires_at"]),
            responded_at=_parse(row["responded_at"]),
            applied_at=_parse(row["applied_at"]),
            error=row["error"],
        )

    def create(self, action: PendingAction) -> PendingAction:
        cursor = self._conn.execute(
            """
            INSERT INTO actions
                (code, kind, summary, reason, payload, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action.code, str(action.kind), action.summary, action.reason,
                json.dumps(action.payload), str(action.status),
                _iso(action.created_at), _iso(action.expires_at),
            ),
        )
        self._conn.commit()
        action.action_id = cursor.lastrowid
        return action

    def get(self, action_id: int) -> PendingAction | None:
        row = self._conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        return self._row_to_action(row) if row else None

    def by_code(self, code: str) -> PendingAction | None:
        """Most recent action with this code, whatever its status.

        Looking up regardless of status matters: a reply to an already-answered
        or expired message should be told so, not silently ignored.
        """
        row = self._conn.execute(
            "SELECT * FROM actions WHERE code = ? ORDER BY id DESC LIMIT 1",
            (code.upper(),),
        ).fetchone()
        return self._row_to_action(row) if row else None

    def pending(self) -> list[PendingAction]:
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE status = ? ORDER BY id DESC",
            (str(ActionStatus.PENDING),),
        ).fetchall()
        return [self._row_to_action(r) for r in rows]

    def recent(self, limit: int = 25) -> list[PendingAction]:
        rows = self._conn.execute(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_action(r) for r in rows]

    def set_status(
        self,
        action_id: int,
        status: ActionStatus,
        *,
        responded: bool = False,
        applied: bool = False,
        error: str | None = None,
    ) -> None:
        now = _iso(dt.datetime.now(dt.UTC))
        self._conn.execute(
            """
            UPDATE actions
               SET status       = ?,
                   responded_at = COALESCE(?, responded_at),
                   applied_at   = COALESCE(?, applied_at),
                   error        = COALESCE(?, error)
             WHERE id = ?
            """,
            (
                str(status),
                now if responded else None,
                now if applied else None,
                error,
                action_id,
            ),
        )
        self._conn.commit()

    def expire_stale(self) -> int:
        """Mark past-deadline pending actions expired. Returns how many."""
        now = _iso(dt.datetime.now(dt.UTC))
        cursor = self._conn.execute(
            "UPDATE actions SET status = ? WHERE status = ? AND expires_at <= ?",
            (str(ActionStatus.EXPIRED), str(ActionStatus.PENDING), now),
        )
        self._conn.commit()
        return cursor.rowcount

    def has_open_duplicate(self, kind: ActionKind, payload_key: str) -> bool:
        """
        Whether an equivalent proposal is already outstanding.

        Without this the scheduler re-proposes the same swap on every run and
        the phone buzzes every ten minutes -- which is how an agent gets muted.
        """
        rows = self._conn.execute(
            "SELECT payload FROM actions WHERE kind = ? AND status = ?",
            (str(kind), str(ActionStatus.PENDING)),
        ).fetchall()
        return any(payload_key in row["payload"] for row in rows)
