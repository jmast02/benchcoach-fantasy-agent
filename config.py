"""Settings, loaded from the environment / .env."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BENCHCOACH_", env_file=".env", extra="ignore"
    )

    # ANTHROPIC_API_KEY has no BENCHCOACH_ prefix, so it needs an explicit
    # alias to be picked up from the same .env file.
    model_config["populate_by_name"] = True

    league_id: int | None = None
    season: int = 2026
    team_id: int | None = None

    # Private-league session cookies. Full account access, not scoped tokens --
    # never log these, never render them, never send them anywhere but ESPN.
    espn_s2: str | None = None
    swid: str | None = None

    # The master safety switch. While true, no request is ever sent to ESPN's
    # write host; proposed changes are recorded and displayed only.
    dry_run: bool = True

    # The agent's model. Opus for judgement calls; this is the only paid
    # component in the project.
    agent_model: str = "claude-opus-5"

    # Read through settings rather than left to the SDK's environment lookup.
    # Under launchd there is no shell, nothing sources .env, and os.environ is
    # bare -- so a key that works in every manual test disappears the moment
    # the scheduler runs it for real.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # --- Telegram (preferred: free, instant, real buttons) ------------------
    # Replies arrive by long-polling, so unlike SMS this needs no public URL
    # and no carrier registration.
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    # --- SMS (optional; without it the console channel is used) -------------
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    my_phone_number: str | None = None

    # Public URL Twilio posts replies to, e.g.
    #   https://<tunnel>.trycloudflare.com/sms/inbound
    # Must match exactly, including scheme and path: Twilio signs the full URL,
    # so a mismatch fails signature validation rather than failing open.
    public_base_url: str | None = None

    # How long an approval request stays valid. Past this it is expired rather
    # than applied -- a lineup change after kickoff is worse than none.
    approval_ttl_minutes: int = 90

    @property
    def sms_configured(self) -> bool:
        return all(
            [
                self.twilio_account_sid,
                self.twilio_auth_token,
                self.twilio_from_number,
                self.my_phone_number,
            ]
        )

    @property
    def resolved_anthropic_key(self) -> str | None:
        """The key from .env, falling back to the ambient environment."""
        import os

        return self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def has_credentials(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    @property
    def configured(self) -> bool:
        return self.league_id is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
