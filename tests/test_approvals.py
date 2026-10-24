"""
The approval loop.

The safety property under test: nothing is executable unless a human approved
it and the deadline has not passed. Everything else here protects the quality
of that human interaction -- misreading a reply is the one bug that silently
changes someone's lineup.
"""

import datetime as dt
import re

import pytest

from approvals.channels import ConsoleChannel, validate_twilio_signature
from approvals.models import (
    ActionKind,
    ActionStatus,
    LineupSwap,
    PendingAction,
    _code,
)
from approvals.service import ApprovalService, Intent, parse_reply
from approvals.store import ActionStore

CODE_RE = re.compile(r"^[234679][234679ACDEFHJKMNPQRTUVWXY]{2}$")


@pytest.fixture
def service(tmp_path):
    store = ActionStore(tmp_path / "t.db")
    yield ApprovalService(store, ConsoleChannel())
    store.close()


def _swap(out_id=1, in_id=2):
    return LineupSwap(
        out_player_id=out_id, out_name="Mahomes", out_slot=0, out_projected=15.6,
        in_player_id=in_id, in_name="Lamar", in_slot=20, in_projected=19.9,
    )


def _later(minutes=90):
    return dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)


# --------------------------------------------------------------- code format

def test_every_generated_code_matches_the_parser_regex():
    """
    Round-trip property.

    An earlier version could mint codes the parser could not match, because
    the generator's alphabet and the regex had drifted apart. Generating many
    and matching them all is the check that keeps them in sync.
    """
    assert all(CODE_RE.match(_code()) for _ in range(500))


def test_codes_never_look_like_words():
    """Codes start with a digit so a reply like NAH cannot be read as one."""
    assert all(_code()[0].isdigit() for _ in range(200))


# ------------------------------------------------------------ reply parsing

@pytest.mark.parametrize(
    "text,expected",
    [
        ("YES", Intent.APPROVE), ("y", Intent.APPROVE), ("ok", Intent.APPROVE),
        ("do it", Intent.APPROVE), ("yes please", Intent.APPROVE),
        ("YEP do it", Intent.APPROVE), ("👍", Intent.APPROVE), ("1", Intent.APPROVE),
        ("no", Intent.REJECT), ("NAH", Intent.REJECT), ("skip", Intent.REJECT),
        ("no thanks", Intent.REJECT), ("dont do it", Intent.REJECT),
        ("leave it", Intent.REJECT), ("0", Intent.REJECT),
        ("what", Intent.UNCLEAR), ("maybe later", Intent.UNCLEAR),
        ("", Intent.UNCLEAR), ("   ", Intent.UNCLEAR),
    ],
)
def test_reply_intent(text, expected):
    assert parse_reply(text).intent is expected


def test_negative_phrase_beats_an_affirmative_substring():
    """'dont do it' contains 'do it' and must still be a rejection."""
    assert parse_reply("dont do it").intent is Intent.REJECT
    assert parse_reply("do not do it").intent is Intent.REJECT


def test_code_is_extracted_alongside_intent():
    assert parse_reply("YES 7F2").code == "7F2"
    assert parse_reply("no 4KQ").code == "4KQ"
    assert parse_reply("DO IT 7F2").intent is Intent.APPROVE
    assert parse_reply("DO IT 7F2").code == "7F2"


# ------------------------------------------------------------ the safety gate

def test_nothing_is_actionable_until_approved(service):
    action = service.propose_swap(_swap(), reason="r", expires_at=_later())
    assert action.status is ActionStatus.PENDING
    assert not action.is_actionable()
    assert service.approved_actions() == []


def test_approved_action_becomes_actionable(service):
    action = service.propose_swap(_swap(), reason="r", expires_at=_later())
    service.handle_reply(f"YES {action.code}")
    assert [a.action_id for a in service.approved_actions()] == [action.action_id]


def test_rejected_action_never_becomes_actionable(service):
    action = service.propose_swap(_swap(), reason="r", expires_at=_later())
    service.handle_reply(f"NO {action.code}")
    assert service.store.get(action.action_id).status is ActionStatus.REJECTED
    assert service.approved_actions() == []


def test_expired_action_cannot_be_approved(service):
    """Approving after kickoff must not execute -- a late change is worse than none."""
    action = PendingAction(
        kind=ActionKind.LINEUP_SWAP,
        summary="s", reason="r", payload=_swap().to_dict(),
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
    )
    service.store.create(action)
    reply = service.handle_reply(f"YES {action.code}")
    assert "too late" in reply.lower()
    assert service.approved_actions() == []


def test_unclear_reply_changes_nothing_and_asks_again(service):
    action = service.propose_swap(_swap(), reason="r", expires_at=_later())
    reply = service.handle_reply("huh?")
    assert action.code in reply
    assert service.store.get(action.action_id).status is ActionStatus.PENDING


# ------------------------------------------------------------- conversation

def test_bare_yes_works_when_only_one_is_pending(service):
    action = service.propose_swap(_swap(), reason="r", expires_at=_later())
    service.handle_reply("yes")
    assert service.store.get(action.action_id).status is ActionStatus.APPROVED


def test_bare_yes_is_refused_when_several_are_pending(service):
    a = service.propose_swap(_swap(1, 2), reason="r", expires_at=_later())
    b = service.propose_swap(_swap(3, 4), reason="r", expires_at=_later())
    reply = service.handle_reply("yes")
    assert a.code in reply and b.code in reply
    assert service.store.get(a.action_id).status is ActionStatus.PENDING
    assert service.store.get(b.action_id).status is ActionStatus.PENDING


def test_duplicate_proposals_are_suppressed(service):
    """Re-proposing the same swap each tick is how an agent gets muted."""
    first = service.propose_swap(_swap(), reason="r", expires_at=_later())
    second = service.propose_swap(_swap(), reason="r", expires_at=_later())
    assert first is not None
    assert second is None
    assert len(service.channel.sent) == 1


def test_answering_twice_is_handled(service):
    action = service.propose_swap(_swap(), reason="r", expires_at=_later())
    service.handle_reply(f"YES {action.code}")
    reply = service.handle_reply(f"YES {action.code}")
    assert "already" in reply.lower()


def test_reply_with_unknown_code(service):
    assert "don't have" in service.handle_reply("YES 999").lower()


def test_reply_when_nothing_is_pending(service):
    assert "nothing" in service.handle_reply("yes").lower()


# ------------------------------------------------------------ webhook guard

def test_twilio_signature_validation():
    token = "secret-token"
    url = "https://example.com/sms/inbound"
    params = {"Body": "YES", "From": "+15555556666"}

    import base64
    import hashlib
    import hmac

    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    good = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()

    assert validate_twilio_signature(token, good, url, params)
    assert not validate_twilio_signature(token, "wrong", url, params)
    assert not validate_twilio_signature(token, "", url, params)
    # A different URL must not validate -- this is what stops replay elsewhere.
    assert not validate_twilio_signature(token, good, url + "x", params)
    # Tampering with the body must invalidate it.
    assert not validate_twilio_signature(
        token, good, url, {**params, "Body": "NO"}
    )
