"""
ESPN fantasy football read client.

This talks to ESPN's **undocumented internal API** -- the same endpoints the
fantasy web app calls. There is no public, supported alternative: Sleeper's
official API is read-only and Yahoo gated programmatic access behind an
application in 2026. So this is a personal automation for your own league, and
it can break without notice when ESPN changes something.

Public leagues need no credentials. Private leagues need the `espn_s2` and
`SWID` session cookies, which are full account access rather than scoped
tokens -- they are read from the environment and never logged.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from espn.models import (
    DEDICATED_SLOT,
    ELIGIBLE_SLOTS,
    League,
    Matchup,
    Player,
    Position,
    Slot,
    Team,
)

logger = logging.getLogger(__name__)

READ_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"

# statSourceId distinguishes what ESPN is telling you about a week:
#   0 = what actually happened, 1 = what it projects will happen.
STAT_ACTUAL = 0
STAT_PROJECTED = 1


class ESPNError(RuntimeError):
    pass


class ESPNAuthError(ESPNError):
    """Credentials missing, expired, or not valid for this league."""


class ESPNClient:
    def __init__(
        self,
        league_id: int,
        season: int,
        *,
        espn_s2: str | None = None,
        swid: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.league_id = league_id
        self.season = season
        cookies = {}
        if espn_s2 and swid:
            cookies = {"espn_s2": espn_s2, "SWID": swid}
        self._authenticated = bool(cookies)
        self._week_cache: int | None = None
        self._client = httpx.Client(
            cookies=cookies,
            timeout=timeout,
            headers={
                # ESPN rejects requests without a browser-ish UA.
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
        )

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def league_url(self) -> str:
        return f"{READ_BASE}/{self.season}/segments/0/leagues/{self.league_id}"

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ raw

    def _get(self, params: list[tuple[str, str]] | None = None, **kwargs) -> dict[str, Any]:
        try:
            response = self._client.get(self.league_url, params=params, **kwargs)
        except httpx.HTTPError as exc:
            raise ESPNError(f"could not reach ESPN: {exc}") from exc

        if response.status_code in (401, 403):
            raise ESPNAuthError(
                "ESPN refused the request. For a private league set "
                "BENCHCOACH_ESPN_S2 and BENCHCOACH_SWID in .env; if they are "
                "set, they have probably expired -- log in to ESPN again and "
                "copy fresh ones."
            )
        if response.status_code == 404:
            raise ESPNError(
                f"league {self.league_id} not found for season {self.season}. "
                "Check the leagueId in your league URL and the season year."
            )
        response.raise_for_status()
        return response.json()

    DEFAULT_VIEWS = ("mTeam", "mRoster", "mMatchup", "mSettings")

    def raw_league(self, views: tuple[str, ...] = DEFAULT_VIEWS) -> dict:
        return self._get(params=[("view", v) for v in views])

    # --------------------------------------------------------------- parse

    @staticmethod
    def _points(player: dict, week: int, source: int) -> float | None:
        """Pull a week's projected or actual total out of the stats array."""
        for stat in player.get("stats") or []:
            if (
                stat.get("scoringPeriodId") == week
                and stat.get("statSourceId") == source
            ):
                total = stat.get("appliedTotal")
                return round(float(total), 2) if total is not None else None
        return None

    @classmethod
    def _parse_player(cls, entry: dict, week: int) -> Player | None:
        pool = entry.get("playerPoolEntry") or {}
        raw = pool.get("player") or {}
        player_id = raw.get("id") or entry.get("playerId")
        if player_id is None:
            return None

        position_id = raw.get("defaultPositionId")
        try:
            position = Position(position_id) if position_id is not None else None
        except ValueError:
            position = None  # IDP and other positions we do not model yet

        eligible = frozenset(
            Slot(s) for s in (raw.get("eligibleSlots") or []) if s in Slot._value2member_map_
        )
        if not eligible and position is not None:
            eligible = ELIGIBLE_SLOTS.get(position, frozenset())

        return Player(
            player_id=int(player_id),
            name=raw.get("fullName") or "Unknown",
            position=position,
            slot=Slot(entry.get("lineupSlotId", Slot.BENCH)),
            pro_team_id=raw.get("proTeamId", 0),
            injury_status=raw.get("injuryStatus"),
            projected_points=cls._points(raw, week, STAT_PROJECTED),
            actual_points=cls._points(raw, week, STAT_ACTUAL),
            percent_owned=(raw.get("ownership") or {}).get("percentOwned"),
            eligible_slots=eligible,
        )

    @staticmethod
    def _team_name(team: dict) -> str:
        # ESPN moved team naming around between seasons; try the modern field
        # first, then the older location/nickname pair.
        name = (team.get("name") or "").strip()
        if name:
            return name
        pair = f"{team.get('location', '')} {team.get('nickname', '')}".strip()
        return pair or f"Team {team.get('id')}"

    def free_agents(
        self,
        *,
        positions: tuple[Position, ...] | None = None,
        limit: int = 50,
        week: int | None = None,
    ) -> list[Player]:
        """
        Players available to claim: free agents and those on waivers.

        ESPN exposes this through `kona_player_info`, filtered by an
        `x-fantasy-filter` header rather than query parameters -- a JSON blob
        in a header, which is unusual enough to be worth naming. Sorted by
        percent-owned, which is a decent proxy for "who the rest of the world
        has already noticed".
        """
        import json

        slot_ids = [
            int(DEDICATED_SLOT[p]) for p in (positions or ()) if p in DEDICATED_SLOT
        ]

        players_filter: dict = {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": limit,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }
        if slot_ids:
            players_filter["filterSlotIds"] = {"value": sorted(set(slot_ids))}

        data = self._get(
            params=[("view", "kona_player_info")],
            headers={"x-fantasy-filter": json.dumps({"players": players_filter})},
        )

        # The free-agent response carries no scoringPeriodId, so the caller
        # must supply the week -- defaulting to 1 silently yielded 0.0
        # projections for everyone.
        target_week = week or data.get("scoringPeriodId") or self._current_week()
        out: list[Player] = []
        for entry in data.get("players") or []:
            # Free-agent entries nest the player one level shallower than
            # roster entries do, so reuse the parser with a matching shape.
            player = self._parse_player(
                {"playerPoolEntry": entry, "lineupSlotId": int(Slot.BENCH)},
                target_week,
            )
            if player:
                out.append(player)
        return out

    def _current_week(self) -> int:
        """Cached current scoring period, fetched once per client."""
        if self._week_cache is None:
            data = self._get(params=[("view", "mSettings")])
            self._week_cache = int(data.get("scoringPeriodId") or 1)
        return self._week_cache

    def league(self) -> League:
        """Fetch and parse the whole league: teams, rosters, matchups, settings."""
        data = self.raw_league()

        week = data.get("scoringPeriodId") or 1
        settings = data.get("settings") or {}

        slot_counts: dict[Slot, int] = {}
        for raw_slot, count in (
            (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
        ).items():
            if count and int(raw_slot) in Slot._value2member_map_:
                slot_counts[Slot(int(raw_slot))] = int(count)

        # Owner display names live in a separate members array, keyed by SWID.
        members = {
            m.get("id"): (m.get("displayName") or m.get("firstName") or "")
            for m in data.get("members") or []
        }

        teams: list[Team] = []
        for raw_team in data.get("teams") or []:
            overall = (raw_team.get("record") or {}).get("overall") or {}
            owner_ids = raw_team.get("owners") or []
            owner = next((members.get(o) for o in owner_ids if members.get(o)), None)

            roster_entries = (raw_team.get("roster") or {}).get("entries") or []
            roster = [
                p for p in (self._parse_player(e, week) for e in roster_entries) if p
            ]

            teams.append(
                Team(
                    team_id=raw_team.get("id"),
                    name=self._team_name(raw_team),
                    abbrev=(raw_team.get("abbrev") or "").strip(),
                    owner=owner,
                    wins=overall.get("wins", 0),
                    losses=overall.get("losses", 0),
                    ties=overall.get("ties", 0),
                    points_for=round(float(overall.get("pointsFor") or 0), 2),
                    points_against=round(float(overall.get("pointsAgainst") or 0), 2),
                    roster=roster,
                    acquisition_budget_spent=raw_team.get("transactionCounter", {}).get(
                        "acquisitionBudgetSpent"
                    ),
                    waiver_rank=raw_team.get("waiverRank"),
                )
            )

        matchups: list[Matchup] = []
        for game in data.get("schedule") or []:
            home = game.get("home") or {}
            away = game.get("away") or {}
            if not home:
                continue
            matchups.append(
                Matchup(
                    week=game.get("matchupPeriodId", 0),
                    home_team_id=home.get("teamId"),
                    away_team_id=away.get("teamId"),
                    home_score=round(float(home.get("totalPoints") or 0), 2),
                    away_score=round(float(away.get("totalPoints") or 0), 2),
                )
            )

        schedule_settings = settings.get("scheduleSettings") or {}
        return League(
            league_id=self.league_id,
            season=self.season,
            name=(settings.get("name") or f"League {self.league_id}").strip(),
            current_week=week,
            teams=teams,
            matchups=matchups,
            roster_slots=slot_counts,
            playoff_teams=int(schedule_settings.get("playoffTeamCount") or 6),
            regular_season_weeks=int(
                schedule_settings.get("matchupPeriodCount")
                or (max((m.week for m in matchups), default=14))
            ),
        )
