"""
The things a projection does not know.

ESPN's projection is roughly a season average nudged for recent form. It does
not know that your RB faces the league's stingiest run defense, that his team
is a ten-point underdog and will be throwing all afternoon, or that the player
projected two points lower has twice the ceiling -- which is what you want when
you are losing by thirty.

Three signals, all free:

  Matchup   -- fantasy points each defense allows to each position
  Script    -- Vegas spread and total, which imply how a game will be played
  Shape     -- a player's week-to-week volatility: floor versus ceiling

None of this replaces the projection. It explains when the projection is
misleading, which is exactly the judgement call worth handing to a model.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from functools import lru_cache

import polars as pl

from analysis.schedule import resolve_team
from stats.nflverse import CURRENT_SEASON, last_complete_week, schedules, weekly_stats

POSITIONS = ("QB", "RB", "WR", "TE")


# ------------------------------------------------------- defense vs position

@lru_cache(maxsize=4)
def defense_vs_position(season: int = CURRENT_SEASON) -> dict[tuple[str, str], float]:
    """
    PPR points each defense allows per game to each position.

    Computed from what opposing players actually scored against them, which is
    the only definition that matters for a start-sit decision.
    """
    stats = weekly_stats(season)
    needed = {"opponent_team", "position", "fantasy_points_ppr", "week"}
    if not needed.issubset(set(stats.columns)):
        return {}

    frame = stats.filter(
        pl.col("opponent_team").is_not_null()
        & pl.col("position").is_in(list(POSITIONS))
        & pl.col("fantasy_points_ppr").is_not_null()
    )
    per_game = (
        frame.group_by(["opponent_team", "position", "week"])
        .agg(pl.col("fantasy_points_ppr").sum().alias("allowed"))
        .group_by(["opponent_team", "position"])
        .agg(pl.col("allowed").mean().alias("ppg"))
    )
    return {
        (row["opponent_team"], row["position"]): round(row["ppg"], 1)
        for row in per_game.iter_rows(named=True)
    }


@lru_cache(maxsize=4)
def defense_ranks(season: int = CURRENT_SEASON) -> dict[tuple[str, str], int]:
    """Rank 1 = toughest defense against that position."""
    table = defense_vs_position(season)
    ranks: dict[tuple[str, str], int] = {}
    for position in POSITIONS:
        rows = sorted(
            ((team, ppg) for (team, pos), ppg in table.items() if pos == position),
            key=lambda kv: kv[1],
        )
        for index, (team, _) in enumerate(rows, start=1):
            ranks[(team, position)] = index
    return ranks


def matchup_difficulty(opponent: str, position: str, season: int = CURRENT_SEASON) -> dict | None:
    """How hard a given defense is for a given position."""
    abbr = resolve_team(opponent)
    if not abbr:
        return None
    pos = position.upper().replace("D/ST", "DST")
    allowed = defense_vs_position(season).get((abbr, pos))
    rank = defense_ranks(season).get((abbr, pos))
    if allowed is None or rank is None:
        return None

    total = sum(1 for (_, p) in defense_ranks(season) if p == pos)
    if rank <= total * 0.25:
        label = "tough"
    elif rank >= total * 0.75:
        label = "soft"
    else:
        label = "neutral"
    return {
        "opponent": abbr, "position": pos, "points_allowed_per_game": allowed,
        "rank": rank, "of": total, "label": label,
    }


# ------------------------------------------------------------- game script

@dataclass
class GameScript:
    team: str
    opponent: str
    week: int
    home: bool
    spread: float | None          # negative = this team favoured
    total: float | None
    implied_points: float | None

    @property
    def label(self) -> str:
        """
        What the betting market expects the game to look like.

        A heavy underdog throws, which suppresses rushing volume and inflates
        pass-catching work. A heavy favourite runs out the clock. This is the
        single best free predictor of how volume gets distributed.
        """
        if self.spread is None:
            return "unknown"
        if self.spread <= -7:
            return "heavy favourite — expect run-heavy, clock-killing script"
        if self.spread <= -3:
            return "favoured — mildly run-leaning"
        if self.spread >= 7:
            return "heavy underdog — expect pass-heavy catch-up script"
        if self.spread >= 3:
            return "underdog — mildly pass-leaning"
        return "close game — neutral script"

    def describe(self) -> str:
        where = "vs" if self.home else "@"
        bits = [f"wk{self.week} {where} {self.opponent}"]
        if self.spread is not None:
            bits.append(f"spread {self.spread:+.1f}")
        if self.total is not None:
            bits.append(f"total {self.total}")
        if self.implied_points is not None:
            bits.append(f"implied {self.implied_points:.1f} pts")
        return ", ".join(bits) + f" — {self.label}"


def game_script(
    team: str, *, week: int | None = None, season: int = CURRENT_SEASON
) -> GameScript | None:
    """Vegas-implied context for a team's game in a given week."""
    abbr = resolve_team(team)
    if not abbr:
        return None
    target = week if week is not None else last_complete_week(season) + 1

    frame = schedules(season).filter(
        (pl.col("week") == target)
        & ((pl.col("home_team") == abbr) | (pl.col("away_team") == abbr))
    )
    if not frame.height:
        return None
    row = frame.to_dicts()[0]

    home = row["home_team"] == abbr
    spread_line = row.get("spread_line")   # home-relative: positive = home favoured
    total_line = row.get("total_line")

    # Normalise so negative always means "this team is favoured".
    spread = None
    if spread_line is not None:
        spread = -float(spread_line) if home else float(spread_line)

    implied = None
    if total_line is not None and spread is not None:
        implied = round(float(total_line) / 2 - spread / 2, 1)

    return GameScript(
        team=abbr,
        opponent=row["away_team"] if home else row["home_team"],
        week=target, home=home, spread=spread,
        total=float(total_line) if total_line is not None else None,
        implied_points=implied,
    )


# ------------------------------------------------------- floor and ceiling

@dataclass
class Shape:
    """A player's week-to-week distribution, not his average."""

    games: int
    mean: float
    floor: float      # worst game
    ceiling: float    # best game
    volatility: float # stdev

    @property
    def label(self) -> str:
        if self.games < 3:
            return "unknown"
        if self.volatility >= self.mean * 0.6:
            return "boom-or-bust"
        if self.volatility <= self.mean * 0.3:
            return "steady"
        return "moderate"


def player_shape(gsis_id: str | None, *, season: int = CURRENT_SEASON) -> Shape | None:
    """Floor, ceiling and volatility from this season's weekly scores."""
    if not gsis_id:
        return None
    frame = weekly_stats(season).filter(
        (pl.col("player_id") == gsis_id) & pl.col("fantasy_points_ppr").is_not_null()
    )
    values = [float(v) for v in frame["fantasy_points_ppr"].to_list()]
    if not values:
        return None
    return Shape(
        games=len(values),
        mean=round(statistics.mean(values), 1),
        floor=round(min(values), 1),
        ceiling=round(max(values), 1),
        volatility=round(statistics.pstdev(values), 1) if len(values) > 1 else 0.0,
    )
