"""
Pending actions: the things the agent wants to do but may not do yet.

Every state-changing operation becomes one of these first. Nothing reaches
ESPN's write host except by way of an action that was created, sent, approved
by a human, and not yet expired. That chain is the safety property of the whole
system, so it is modelled explicitly rather than implied by control flow.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass, field
from enum import StrEnum


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    APPLIED = "applied"
    FAILED = "failed"


class ActionKind(StrEnum):
    LINEUP_SWAP = "lineup_swap"


# Easily-confused characters removed: no 0/O, 1/I/L, 5/S, 8/B, Z/2. A code gets
# typed on a phone, possibly in a hurry, possibly in a bar.
# The first character is drawn from _CODE_DIGITS, the rest from _CODE_CHARS.
# _CODE_CHARS must be a superset of _CODE_DIGITS, or codes can be generated
# that the matching regex cannot recognise -- "7F2" was mintable but unmatchable
# while 2 was missing here.
_CODE_DIGITS = "234679"
_CODE_CHARS = "234679ACDEFHJKMNPQRTUVWXY"


def _code() -> str:
    """
    Short human-typable code, e.g. "7F2".

    Disambiguates replies when more than one approval is outstanding.

    The first character is always a digit, and that is load-bearing rather than
    cosmetic: an all-letter code collides with how people actually reply. An
    earlier version could mint "NAH" and "YEP", so a user typing "NAH" was told
    "I don't have anything with code NAH" instead of having their rejection
    understood. Starting with a digit makes a code structurally unable to look
    like a word.
    """
    return secrets.choice(_CODE_DIGITS) + "".join(
        secrets.choice(_CODE_CHARS) for _ in range(2)
    )


@dataclass
class LineupSwap:
    """Bench `player_out`, start `player_in`, in `player_out`'s slot."""

    out_player_id: int
    out_name: str
    out_slot: int
    out_projected: float
    in_player_id: int
    in_name: str
    in_slot: int
    in_projected: float

    @property
    def gain(self) -> float:
        return round(self.in_projected - self.out_projected, 1)

    def to_dict(self) -> dict:
        return {
            "out_player_id": self.out_player_id, "out_name": self.out_name,
            "out_slot": self.out_slot, "out_projected": self.out_projected,
            "in_player_id": self.in_player_id, "in_name": self.in_name,
            "in_slot": self.in_slot, "in_projected": self.in_projected,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LineupSwap:
        return cls(**data)


@dataclass
class PendingAction:
    kind: ActionKind
    summary: str
    reason: str
    payload: dict
    expires_at: dt.datetime
    action_id: int | None = None
    code: str = field(default_factory=_code)
    status: ActionStatus = ActionStatus.PENDING
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    responded_at: dt.datetime | None = None
    applied_at: dt.datetime | None = None
    error: str | None = None

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        return (now or dt.datetime.now(dt.UTC)) >= self.expires_at

    def is_actionable(self, now: dt.datetime | None = None) -> bool:
        """Approved, not expired, not already applied -- the only state that may write."""
        return self.status is ActionStatus.APPROVED and not self.is_expired(now)

    @property
    def swap(self) -> LineupSwap | None:
        if self.kind is ActionKind.LINEUP_SWAP:
            return LineupSwap.from_dict(self.payload)
        return None

    def message(self) -> str:
        """The SMS body. Short, specific, and it says how to answer."""
        minutes = max(
            0, int((self.expires_at - dt.datetime.now(dt.UTC)).total_seconds() // 60)
        )
        window = f"{minutes}m" if minutes < 90 else f"{minutes // 60}h"
        return (
            f"{self.summary}\n\n{self.reason}\n\n"
            f"Reply YES {self.code} to do it, NO {self.code} to skip. "
            f"Expires in {window}."
        )
