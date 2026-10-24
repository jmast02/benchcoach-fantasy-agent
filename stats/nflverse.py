"""
nflverse data access, and the bridge from ESPN player ids to it.

ESPN and nflverse name the same human differently and key them differently, so
joining them is the first problem. Rather than fuzzy-matching names -- which
quietly mismatches a Michael Thomas or a Josh Allen -- this uses the ffverse
crosswalk, which carries an explicit `espn_id` alongside `gsis_id` and
`pfr_id`. Measured on a real 10-team league: 169 of 169 individual players
matched exactly. The 13 misses were D/ST units, which are teams, not people,
and are handled separately.

Everything is cached on disk by nflreadpy and memoised per process. These are
multi-megabyte parquet releases; re-fetching them on every page load would make
the app unusable.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import nflreadpy as nfl
import polars as pl

logger = logging.getLogger(__name__)

CURRENT_SEASON = 2026


@lru_cache(maxsize=4)
def crosswalk() -> dict[int, dict]:
    """ESPN player id -> {gsis_id, pfr_id, name, position, team}."""
    table = nfl.load_ff_playerids()
    table = table.filter(pl.col("espn_id").is_not_null())
    keep = [
        c
        for c in ("espn_id", "gsis_id", "pfr_id", "name", "position", "team")
        if c in table.columns
    ]
    return {int(row["espn_id"]): row for row in table.select(keep).iter_rows(named=True)}


@lru_cache(maxsize=4)
def weekly_stats(season: int = CURRENT_SEASON) -> pl.DataFrame:
    """Per-player, per-week usage and production, keyed on gsis_id."""
    return nfl.load_player_stats(seasons=[season])


@lru_cache(maxsize=4)
def snap_counts(season: int = CURRENT_SEASON) -> pl.DataFrame:
    """Per-player, per-week snap percentage, keyed on pfr_player_id."""
    return nfl.load_snap_counts(seasons=[season])


@lru_cache(maxsize=4)
def injury_reports(season: int = CURRENT_SEASON) -> pl.DataFrame:
    """
    Official weekly injury reports.

    Richer than ESPN's single `injuryStatus` string: it carries both the game
    status and *practice participation*. "Questionable + Did Not Participate"
    and "Questionable + Full Participation" carry the same ESPN tag and mean
    very different things, and that gap is where the agent's judgement lives.
    """
    return nfl.load_injuries(seasons=[season])


@lru_cache(maxsize=4)
def schedules(season: int = CURRENT_SEASON) -> pl.DataFrame:
    """Game-by-game schedule, used for matchup lookahead and bye weeks."""
    return nfl.load_schedules(seasons=[season])


def latest_week(season: int = CURRENT_SEASON) -> int:
    """The highest week with any data, complete or not."""
    stats = weekly_stats(season)
    return int(stats["week"].max()) if stats.height else 0


@lru_cache(maxsize=4)
def last_complete_week(season: int = CURRENT_SEASON) -> int:
    """
    The last week whose games have all finished.

    Trend analysis must anchor here rather than on the current scoring period.
    A week in progress contains results for only the teams that have already
    played -- on a Thursday, one game of fourteen -- so including it gives a
    handful of players a "recent" window built from a single game while
    everyone else gets none. Since the agent runs mid-week by design, that is
    the normal case, not an edge case.
    """
    games = schedules(season)
    if not games.height or "result" not in games.columns:
        return latest_week(season)

    by_week = (
        games.filter(pl.col("game_type") == "REG")
        .group_by("week")
        .agg(
            [
                pl.len().alias("games"),
                pl.col("result").is_not_null().sum().alias("final"),
            ]
        )
        .sort("week")
    )
    complete = [
        int(row["week"]) for row in by_week.iter_rows(named=True)
        if row["games"] and row["final"] == row["games"]
    ]
    return max(complete) if complete else 0


def injury_detail(gsis_id: str | None, week: int, season: int = CURRENT_SEASON) -> dict | None:
    """The most recent injury-report row for a player at or before `week`."""
    if not gsis_id:
        return None
    reports = injury_reports(season)
    if "gsis_id" not in reports.columns:
        return None
    rows = reports.filter(
        (pl.col("gsis_id") == gsis_id) & (pl.col("week") <= week)
    ).sort("week")
    if not rows.height:
        return None
    return rows.tail(1).to_dicts()[0]


def warm_cache(season: int = CURRENT_SEASON) -> None:
    """Pull everything once so the first request isn't slow."""
    crosswalk()
    weekly_stats(season)
    snap_counts(season)
    injury_reports(season)
    schedules(season)
