"""Settings, loaded from the environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BENCHCOACH_", env_file=".env", extra="ignore"
    )

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

    @property
    def has_credentials(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    @property
    def configured(self) -> bool:
        return self.league_id is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
