"""
The approval loop: propose, ask, interpret the answer, record it.

The interpretation step is the delicate one. A reply arrives as free text from
a human on a phone, and the cost of the two possible mistakes is wildly
asymmetric: misreading "no" as approval silently changes a lineup, while
failing to understand a genuine "yes" costs one more text. So the parser is
generous about wording and absolutely unwilling to guess -- anything it cannot
read confidently gets a clarifying question, never an assumed answer.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from approvals.channels import Channel
from approvals.models import (
    ActionKind,
    ActionStatus,
    LineupSwap,
    PendingAction,
    WaiverClaim,
)
from approvals.store import ActionStore

logger = logging.getLogger(__name__)

# Deliberately generous. People reply to a text the way they talk.
_AFFIRMATIVE = {
    "y", "ye", "yes", "yep", "yup", "yeah", "yea", "ok", "okay", "k", "sure",
    "do it", "doit", "go", "send it", "sendit", "approve", "approved", "confirm",
    "yes please", "please", "1", "+1", "👍",
}
_NEGATIVE = {
    "n", "no", "nope", "nah", "naw", "dont", "don't", "do not", "skip", "pass",
    "reject", "cancel", "leave it", "no thanks", "nothanks", "0", "-1", "👎",
}

# Must start with a digit -- see approvals.models._code for why.
_CODE = re.compile(r"\b([234679][234679ACDEFHJKMNPQRTUVWXY]{2})\b", re.IGNORECASE)

# Multi-word replies ("do it", "no thanks") have to match as a prefix, not only
# as the whole string, or any trailing chatter defeats them. Longest first so
# "no thanks" wins over "no".
_AFFIRMATIVE_PHRASES = sorted(
    (p for p in _AFFIRMATIVE if " " in p), key=len, reverse=True
)
_NEGATIVE_PHRASES = sorted(
    (p for p in _NEGATIVE if " " in p), key=len, reverse=True
)


class Intent(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    UNCLEAR = "unclear"


@dataclass
class ParsedReply:
    intent: Intent
    code: str | None


def parse_reply(text: str) -> ParsedReply:
    """Read a human's SMS reply into an intent and (maybe) an action code."""
    raw = (text or "").strip()
    if not raw:
        return ParsedReply(Intent.UNCLEAR, None)

    normalized = re.sub(r"[^a-z0-9'+\- 👍👎]", " ", raw.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # Intent first, from the text with any code removed. Reading intent before
    # extracting a code keeps a bare word reply ("nah") from being mistaken for
    # a code, whatever the code alphabet happens to be.
    without_code = _CODE.sub(" ", normalized)
    without_code = re.sub(r"\s+", " ", without_code).strip()

    def classify(candidate: str) -> Intent:
        if not candidate:
            return Intent.UNCLEAR
        if candidate in _AFFIRMATIVE:
            return Intent.APPROVE
        if candidate in _NEGATIVE:
            return Intent.REJECT
        # Multi-word phrase with trailing chatter: "do it please".
        for phrase in _NEGATIVE_PHRASES:
            if candidate.startswith(phrase + " "):
                return Intent.REJECT
        for phrase in _AFFIRMATIVE_PHRASES:
            if candidate.startswith(phrase + " "):
                return Intent.APPROVE
        # Single leading keyword: "yes please do it".
        head = candidate.split(" ")[0]
        if head in _NEGATIVE:
            return Intent.REJECT
        if head in _AFFIRMATIVE:
            return Intent.APPROVE
        return Intent.UNCLEAR

    intent = classify(without_code)
    if intent is Intent.UNCLEAR and without_code != normalized:
        # The stripped token may have been part of the words, not a code.
        intent = classify(normalized)

    code_match = _CODE.search(normalized)
    code = code_match.group(1).upper() if code_match else None
    return ParsedReply(intent, code)


class ApprovalService:
    def __init__(self, store: ActionStore, channel: Channel) -> None:
        self.store = store
        self.channel = channel

    # ------------------------------------------------------------- proposing

    def propose_swap(
        self,
        swap: LineupSwap,
        *,
        reason: str,
        expires_at: dt.datetime,
        send: bool = True,
    ) -> PendingAction | None:
        """
        Record a proposed lineup change and (optionally) text about it.

        Returns None when an equivalent proposal is already outstanding --
        re-asking the same question every scheduler tick is how an agent earns
        itself a mute.
        """
        # Sweep expired actions before the duplicate check. Without this an
        # unanswered proposal stays "pending" forever and permanently
        # suppresses every future proposal for that player -- one ignored
        # message would silently retire the agent for that slot.
        self.store.expire_stale()

        key = f'"out_player_id": {swap.out_player_id}'
        if self.store.has_open_duplicate(ActionKind.LINEUP_SWAP, key):
            logger.info("skipping duplicate proposal for %s", swap.out_name)
            return None

        action = PendingAction(
            kind=ActionKind.LINEUP_SWAP,
            # Sign has to be formatted, not assumed positive. Most swaps are
            # upgrades, but an injury swap can legitimately cost projected
            # points -- and "(+-5.2)" is how that looked before this.
            summary=(
                f"Start {swap.in_name} over {swap.out_name} "
                f"({swap.gain:+.1f})"
            ),
            reason=reason,
            payload=swap.to_dict(),
            expires_at=expires_at,
        )
        self.store.create(action)

        if send:
            try:
                # Richer channels render buttons; the rest fall back to text.
                sender = getattr(self.channel, "send_approval", None)
                if callable(sender):
                    sender(action)
                else:
                    self.channel.send(action.message())
            except Exception as exc:
                logger.error("could not send approval request: %s", exc)
                self.store.set_status(
                    action.action_id, ActionStatus.FAILED, error=str(exc)
                )
                raise
        return action

    def propose_claim(
        self,
        claim: WaiverClaim,
        *,
        reason: str,
        expires_at: dt.datetime,
        send: bool = True,
    ) -> PendingAction | None:
        """Record a proposed waiver claim and ask about it."""
        self.store.expire_stale()

        key = f'"add_player_id": {claim.add_player_id}'
        if self.store.has_open_duplicate(ActionKind.WAIVER_CLAIM, key):
            logger.info("skipping duplicate claim for %s", claim.add_name)
            return None

        action = PendingAction(
            kind=ActionKind.WAIVER_CLAIM,
            summary=(
                f"Claim {claim.add_name} ({claim.add_position}), "
                f"drop {claim.drop_name} ({claim.gain:+.1f})"
            ),
            reason=reason,
            payload=claim.to_dict(),
            expires_at=expires_at,
        )
        self.store.create(action)

        if send:
            try:
                sender = getattr(self.channel, "send_approval", None)
                if callable(sender):
                    sender(action)
                else:
                    self.channel.send(action.message())
            except Exception as exc:
                logger.error("could not send claim request: %s", exc)
                self.store.set_status(
                    action.action_id, ActionStatus.FAILED, error=str(exc)
                )
                raise
        return action

    # ------------------------------------------------------------- answering

    def handle_reply(self, text: str) -> str:
        """
        Interpret an inbound reply and return what to say back.

        Always returns something. Silence on an unparsed reply leaves a human
        believing they approved a change that never happened.
        """
        self.store.expire_stale()
        parsed = parse_reply(text)
        pending = self.store.pending()

        if parsed.code:
            action = self.store.by_code(parsed.code)
            if action is None:
                return f"I don't have anything with code {parsed.code}."
            if action.status is ActionStatus.EXPIRED:
                return f"Too late on {action.code} — that one already kicked off."
            if action.status is not ActionStatus.PENDING:
                return f"{action.code} was already {action.status}. Nothing to do."
        elif len(pending) == 1:
            action = pending[0]
        elif not pending:
            return "Nothing is waiting on you right now."
        else:
            codes = ", ".join(a.code for a in pending)
            return (
                f"You have {len(pending)} waiting: {codes}. "
                "Reply with the code too, e.g. YES " + pending[0].code + "."
            )

        if parsed.intent is Intent.UNCLEAR:
            return (
                f'I didn\'t understand "{text.strip()[:40]}". '
                f"Reply YES {action.code} or NO {action.code}."
            )

        if parsed.intent is Intent.REJECT:
            self.store.set_status(action.action_id, ActionStatus.REJECTED, responded=True)
            return f"OK, leaving it. ({action.code})"

        if action.is_expired():
            self.store.set_status(action.action_id, ActionStatus.EXPIRED, responded=True)
            return f"Too late on {action.code} — that one already kicked off."

        self.store.set_status(action.action_id, ActionStatus.APPROVED, responded=True)
        return f"Done — {action.summary}. ({action.code})"

    # -------------------------------------------------------------- applying

    def approved_actions(self) -> list[PendingAction]:
        """Actions cleared to execute: approved, unexpired, not yet applied."""
        self.store.expire_stale()
        return [a for a in self.store.recent(100) if a.is_actionable()]
