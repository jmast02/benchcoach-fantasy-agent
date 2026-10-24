"""
Where approval requests go out and replies come back.

Two implementations behind one interface: Twilio for real SMS, and a console
channel so the entire loop can be developed and tested without an account,
without credentials, and without sending anything to a real phone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Protocol
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

TWILIO_BASE = "https://api.twilio.com/2010-04-01"


class Channel(Protocol):
    name: str

    def send(self, body: str) -> str | None:
        """Deliver a message. Returns a provider id, or None."""
        ...


class ConsoleChannel:
    """Prints instead of sending. The default until Twilio is configured."""

    name = "console"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_approval(self, action) -> str | None:
        return self.send(action.message())

    def send(self, body: str) -> str | None:
        self.sent.append(body)
        print("\n" + "=" * 54)
        print("TEXT (console channel -- nothing was actually sent)")
        print("=" * 54)
        print(body)
        print("=" * 54 + "\n")
        return None


class TwilioSMSChannel:
    """
    Real SMS through Twilio's REST API.

    Uses httpx directly rather than the Twilio SDK -- sending a message is one
    form POST, and this avoids a dependency whose surface is far larger than
    what is needed.
    """

    name = "sms"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.to_number = to_number
        self._client = httpx.Client(timeout=timeout, auth=(account_sid, auth_token))

    def send_approval(self, action) -> str | None:
        # SMS has no buttons; the message body already explains how to reply.
        return self.send(action.message())

    def send(self, body: str) -> str | None:
        url = f"{TWILIO_BASE}/Accounts/{self.account_sid}/Messages.json"
        try:
            response = self._client.post(
                url,
                data={"From": self.from_number, "To": self.to_number, "Body": body},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Twilio returns a useful error code; surface it without the token.
            detail = ""
            try:
                detail = exc.response.json().get("message", "")
            except Exception:
                detail = exc.response.text[:200]
            logger.error("Twilio rejected the message: %s", detail)
            raise RuntimeError(f"Twilio send failed: {detail}") from exc
        return response.json().get("sid")

    def close(self) -> None:
        self._client.close()


def validate_twilio_signature(
    auth_token: str, signature: str, url: str, params: dict[str, str]
) -> bool:
    """
    Verify an inbound webhook really came from Twilio.

    This endpoint can change a lineup, so it is publicly reachable *and*
    state-changing. Without signature validation anyone who learns the URL
    could approve actions. Twilio signs the full URL concatenated with each
    POST parameter name and value, sorted by name, under HMAC-SHA1.
    """
    if not signature or not auth_token:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(
        auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def twiml(message: str | None = None) -> str:
    """A minimal TwiML response. Empty means 'received, say nothing back'."""
    if not message:
        return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    escaped = (
        message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escaped}</Message></Response>"
    )


def build_channel(settings) -> Channel:
    """
    Pick the best configured channel.

    Telegram first: it needs no carrier registration, no public URL, and gives
    real buttons. SMS second, for anyone who has cleared A2P 10DLC. Console
    last, which keeps the whole loop testable with nothing configured at all.
    """
    if getattr(settings, "telegram_configured", False):
        from approvals.telegram import TelegramChannel

        return TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)

    if getattr(settings, "sms_configured", False):
        return TwilioSMSChannel(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_from_number,
            settings.my_phone_number,
        )
    return ConsoleChannel()


__all__ = [
    "Channel",
    "ConsoleChannel",
    "TwilioSMSChannel",
    "build_channel",
    "twiml",
    "urlencode",
    "validate_twilio_signature",
]
