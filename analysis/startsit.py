"""
Start-sit reasoning that goes past the projection, and weekly matchup strategy.

Two ideas drive this.

**A projection is a point estimate of a distribution.** Two players projected
14 and 12 are not ranked if the first has a floor of 4 and a ceiling of 18
while the second is 10-to-30. Which one you want depends entirely on whether
you need a safe 12 or a chance at 30.

**And what you need depends on the matchup.** If you are a 35-point favourite,
variance is your enemy -- take the floor. If you are a 25-point underdog, the
safe play loses slowly; you need the ceiling. Most tools ignore this entirely
and just sort by projection.

The output here is evidence, not a verdict. The call itself is a judgement,
which is what the model is for.
"""

from __future__ import annotations

from dataclasses import dataclass

from analysis.matchup import game_script, matchup_difficulty, player_shape
from espn.models import Player, Team
from stats.nflverse import crosswalk


@dataclass
class PlayerOutlook:
    """Everything known about a player's week beyond his projection."""

    player: Player
    projection: float
    opponent: str | None = None
    script: str | None = None
    implied_points: float | None = None
    matchup_label: str | None = None
    matchup_rank: str | None = None
    points_allowed: float | None = None
    floor: float | None = None
    ceiling: float | None = None
    shape: str | None = None
    trend: str | None = None

    def describe(self) -> str:
        lines = [
            f"{self.player.name} ({self.player.position_name}) "
            f"— projected {self.projection:.1f}"
        ]
        if self.opponent:
            lines.append(f"  opponent: {self.opponent}")
        if self.matchup_label:
            lines.append(
                f"  matchup: {self.matchup_label} "
                f"({self.matchup_rank}, {self.points_allowed} pts/g allowed to the position)"
            )
        if self.script:
            implied = f", team implied {self.implied_points}" if self.implied_points else ""
            lines.append(f"  game script: {self.script}{implied}")
        if self.floor is not None:
            lines.append(
                f"  range this season: floor {self.floor}, ceiling {self.ceiling} ({self.shape})"
            )
        if self.trend:
            lines.append(f"  usage trend: {self.trend}")
        return "\n".join(lines)


def _team_abbr_for(player: Player, trends: dict) -> str | None:
    trend = trends.get(player.player_id)
    if trend and trend.team:
        return trend.team
    row = crosswalk().get(player.player_id)
    return row.get("team") if row else None


def outlook(player: Player, *, week: int, trends: dict | None = None) -> PlayerOutlook:
    """Assemble the non-projection evidence for one player."""
    trends = trends or {}
    result = PlayerOutlook(player=player, projection=player.projected_points or 0.0)

    abbr = _team_abbr_for(player, trends)
    if abbr:
        script = game_script(abbr, week=week)
        if script:
            result.opponent = script.opponent
            result.script = script.label
            result.implied_points = script.implied_points

            difficulty = matchup_difficulty(script.opponent, player.position_name)
            if difficulty:
                result.matchup_label = difficulty["label"]
                result.matchup_rank = f"#{difficulty['rank']} of {difficulty['of']}"
                result.points_allowed = difficulty["points_allowed_per_game"]

    trend = trends.get(player.player_id)
    if trend:
        result.trend = trend.direction()
        shape = player_shape(trend.gsis_id)
        if shape:
            result.floor, result.ceiling, result.shape = (
                shape.floor, shape.ceiling, shape.label
            )
    return result


@dataclass
class MatchupStrategy:
    """Whether this week calls for the safe play or the swing."""

    my_total: float
    their_total: float
    opponent_name: str

    @property
    def margin(self) -> float:
        return round(self.my_total - self.their_total, 1)

    @property
    def stance(self) -> str:
        if self.margin >= 25:
            return "protect"
        if self.margin <= -20:
            return "swing"
        if abs(self.margin) <= 10:
            return "balanced"
        return "lean-safe" if self.margin > 0 else "lean-aggressive"

    def advice(self) -> str:
        if self.stance == "protect":
            return (
                f"You project {self.margin:+.1f} over {self.opponent_name}. "
                "Variance is your enemy — start the steady players and avoid "
                "boom-or-bust swings, even ones projected slightly higher."
            )
        if self.stance == "swing":
            return (
                f"You project {self.margin:+.1f} against {self.opponent_name}. "
                "The safe lineup loses slowly. Prefer high-ceiling players even "
                "at a lower projection — you need the top of their range."
            )
        if self.stance == "balanced":
            return (
                f"Projected within {abs(self.margin):.1f} of {self.opponent_name}. "
                "Close game: take the best expected points and let matchup "
                "quality break ties."
            )
        if self.stance == "lean-safe":
            return (
                f"You project {self.margin:+.1f} ahead of {self.opponent_name}. "
                "Mild edge — prefer the safer option where projections are close."
            )
        return (
            f"You project {self.margin:+.1f} behind {self.opponent_name}. "
            "Lean toward upside where projections are close."
        )


def matchup_strategy(my_team: Team, opponent: Team) -> MatchupStrategy:
    return MatchupStrategy(
        my_total=round(sum(p.projected_points or 0 for p in my_team.starters), 1),
        their_total=round(sum(p.projected_points or 0 for p in opponent.starters), 1),
        opponent_name=opponent.name,
    )
