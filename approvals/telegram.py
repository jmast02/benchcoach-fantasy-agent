"""
Telegram as the approval channel.

Chosen over SMS after US carriers blocked the SMS route: a 10-digit long code
cannot deliver application traffic without A2P 10DLC campaign registration,
which is a paid, multi-day process (Twilio error 30034). Telegram needs a bot
token and nothing else.

It is also a better fit for this job than SMS:

  * Real tappable buttons, so approving is one thumb press rather than typing
    "YES 7JE" correctly while walking into a bar.
  * Replies arrive by long-polling, so there is no public webhook, no tunnel,
    and no signature verification to get wrong.
  * The buttons are removed once answered, so an old message cannot be tapped
    twice and leave you wondering which answer counted.

Free-text replies still work and still go through the same forgiving parser --
the buttons are a convenience, not a requirement.
"""

from __future__ import annotations

import logging

import httpx

from approvals.models import PendingAction

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"


class TelegramChannel:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: int, *, timeout: float = 20.0) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = httpx.Client(timeout=timeout)
        self._offset: int | None = None

    # ------------------------------------------------------------- plumbing

    def _call(self, method: str, **payload):
        url = f"{API}/bot{self.bot_token}/{method}"
        response = self._client.post(url, json=payload)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data.get('description')}")
        return data.get("result")

    def close(self) -> None:
        self._client.close()

    # -------------------------------------------------------------- sending

    def send(self, body: str) -> str | None:
        result = self._call("sendMessage", chat_id=self.chat_id, text=body)
        return str(result.get("message_id")) if result else None

    def send_approval(self, action: PendingAction) -> str | None:
        """Send a proposal with Yes/No buttons carrying the action's code."""
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Do it", "callback_data": f"yes:{action.code}"},
                    {"text": "✕ Skip", "callback_data": f"no:{action.code}"},
                ]
            ]
        }
        result = self._call(
            "sendMessage",
            chat_id=self.chat_id,
            text=action.message(),
            reply_markup=keyboard,
        )
        return str(result.get("message_id")) if result else None

    # ------------------------------------------------------------ receiving

    def poll(self, timeout: int = 25) -> list[dict]:
        """
        Long-poll for new activity.

        Returns normalised events: {"text": str, "message_id": int | None,
        "from_button": bool}. Telegram delivers button taps and typed replies
        in different shapes; both become the same thing here so the approval
        service does not have to care which the human used.
        """
        params = {"timeout": timeout}
        if self._offset is not None:
            params["offset"] = self._offset

        url = f"{API}/bot{self.bot_token}/getUpdates"
        try:
            response = self._client.get(url, params=params, timeout=timeout + 10)
            data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("telegram poll failed: %s", exc)
            return []
        if not data.get("ok"):
            logger.warning("telegram poll rejected: %s", data.get("description"))
            return []

        events: list[dict] = []
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1

            callback = update.get("callback_query")
            if callback:
                # Acknowledge immediately or the client shows a spinner.
                try:
                    self._call("answerCallbackQuery", callback_query_id=callback["id"])
                except RuntimeError:
                    pass
                intent, _, code = (callback.get("data") or "").partition(":")
                message = callback.get("message") or {}
                events.append(
                    {
                        "text": f"{intent} {code}".strip(),
                        "message_id": message.get("message_id"),
                        "from_button": True,
                        "chat_id": (message.get("chat") or {}).get("id"),
                    }
                )
                continue

            message = update.get("message") or update.get("edited_message")
            if message and message.get("text"):
                events.append(
                    {
                        "text": message["text"],
                        "message_id": message.get("message_id"),
                        "from_button": False,
                        "chat_id": (message.get("chat") or {}).get("id"),
                    }
                )
        return events

    def clear_buttons(self, message_id: int) -> None:
        """Strip the buttons off an answered message so it can't be tapped twice."""
        try:
            self._call(
                "editMessageReplyMarkup",
                chat_id=self.chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except RuntimeError as exc:
            logger.debug("could not clear buttons: %s", exc)
