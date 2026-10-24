"""
Schedule lookahead and matchup strength.

This is what makes "grab the Ravens D now, they play Miami in two weeks"
possible. No single lookup answers it: you need who plays whom in coming weeks,
how good those opponents' offenses are, and whether the player is even
available. This module supplies the first two.

Offensive strength is computed from completed game scores rather than a
projection feed, because points actually scored is both freely available and
the thing that matters for streaming a defense.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import nflreadpy as nfl
import polars as pl

from stats.nflverse import CURRENT_SEASON, last_complete_week, schedules


@lru_cache(maxsize=2)
def team_lookup() -> dict[str, str]:
    """Nickname and full name -> nflverse abbreviation ('Ravens' -> 'BAL')."""
    table = nfl.load_teams()
    out: dict[str, str] = {}
    for row in table.iter_rows(named=True):
        abbr = row.get("team_abbr")
        if not abbr:
            continue
        out[abbr.upper()] = abbr
        for key in ("team_nick", "team_name"):
            if row.get(key):
                out[row[key].strip().lower()] = abbr
    return out


def resolve_team(name: str) -> str | None:
    """
    Map an ESPN-style name to an nflverse abbreviation.

    ESPN names defenses like "Ravens D/ST", so the suffix is stripped before
    matching on the nickname.
    """
    if not name:
        return None
    cleaned = name.replace("D/ST", "").replace("DST", "").strip()
    lookup = team_lookup()
    return (
        lookup.get(cleaned.upper())
        or lookup.get(cleaned.lower())
        or lookup.get(cleaned.split()[-1].lower() if cleaned.split() else "")
    )


@dataclass
class Game:
    week: int
    team: str
    opponent: str
    home: bool


def upcoming_games(
    team: str, *, from_week: int | None = None, count: int = 4, season: int = CURRENT_SEASON
) -> list[Game]:
    """The team's next `count` scheduled games, including the current week."""
    abbr = resolve_team(team)
    if not abbr:
        return []
    start = from_week if from_week is not None else last_complete_week(season) + 1

    frame = schedules(season).filter(
        (pl.col("week") >= start)
        & ((pl.col("home_team") == abbr) | (pl.col("away_team") == abbr))
    ).sort("week")

    games: list[Game] = []
    for row in frame.head(count).iter_rows(named=True):
        home = row["home_team"] == abbr
        games.append(
            Game(
                week=int(row["week"]),
                team=abbr,
                opponent=row["away_team"] if home else row["home_team"],
                home=home,
            )
        )
    return games


@lru_cache(maxsize=2)
def offense_strength(season: int = CURRENT_SEASON) -> dict[str, float]:
    """
    Points scored per completed game, by team.

    A low number means a weak offense, which is what makes that team an
    attractive opponent for a streamed defense.
    """
    frame = schedules(season).filter(pl.col("result").is_not_null())
    totals: dict[str, list[int]] = {}
    for row in frame.iter_rows(named=True):
        if row.get("home_score") is None or row.get("away_score") is None:
            continue
        totals.setdefault(row["home_team"], []).append(int(row["home_score"]))
        totals.setdefault(row["away_team"], []).append(int(row["away_score"]))
    return {
        team: round(sum(scores) / len(scores), 1)
        for team, scores in totals.items()
        if scores
    }


@dataclass
class StreamWindow:
    """A defense's upcoming schedule, scored by how weak the opponents are."""

    team: str
    games: list[Game]
    opponent_ppg: list[float]

    @property
    def average_opponent_ppg(self) -> float:
        usable = [p for p in self.opponent_ppg if p is not None]
        return round(sum(usable) / len(usable), 1) if usable else 0.0

    def describe(self) -> str:
        parts = [
            f"wk{g.week} {'vs' if g.home else '@'} {g.opponent} ({ppg} ppg)"
            for g, ppg in zip(self.games, self.opponent_ppg, strict=False)
        ]
        return ", ".join(parts)


def defense_schedule_outlook(
    team: str, *, from_week: int | None = None, count: int = 3, season: int = CURRENT_SEASON
) -> StreamWindow | None:
    """How favourable a defense's next few matchups look."""
    games = upcoming_games(team, from_week=from_week, count=count, season=season)
    if not games:
        return None
    strength = offense_strength(season)
    return StreamWindow(
        team=games[0].team,
        games=games,
        opponent_ppg=[strength.get(g.opponent, 0.0) for g in games],
    )


def best_streaming_defenses(
    candidates: list[str],
    *,
    from_week: int | None = None,
    count: int = 3,
    season: int = CURRENT_SEASON,
) -> list[StreamWindow]:
    """Rank candidate defenses by how weak their upcoming opponents are."""
    windows = [
        w
        for w in (
            defense_schedule_outlook(c, from_week=from_week, count=count, season=season)
            for c in candidates
        )
        if w is not None
    ]
    return sorted(windows, key=lambda w: w.average_opponent_ppg)
