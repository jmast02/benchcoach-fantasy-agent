"""
Live matchup state: what is on the board, and what is still to come.

ESPN's matchup totals read 0.0 until a week finalises, so the live score has
to be summed from each player's actual points instead. That is also the more
useful shape, because it separates the two things that matter mid-week:
points already banked, and points still in play.

"Who is left to play" is the number that decides whether a deficit is a
problem or just a scoreboard artefact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from espn.models import League, Player, Team


@dataclass
class SideState:
    team: Team
    scored: float = 0.0
    yet_to_play: list[Player] = field(default_factory=list)
    played: list[Player] = field(default_factory=list)

    @property
    def remaining_projected(self) -> float:
        return round(sum(p.projected_points or 0 for p in self.yet_to_play), 1)

    @property
    def projected_final(self) -> float:
        return round(self.scored + self.remaining_projected, 1)

    def describe(self) -> str:
        names = ", ".join(
            f"{p.name} ({p.projected_points or 0:.1f})" for p in self.yet_to_play
        )
        return (
            f"{self.team.name}: {self.scored:.1f} on the board, "
            f"{len(self.yet_to_play)} still to play"
            + (f" — {names}" if names else "")
            + f". Projected final {self.projected_final:.1f}."
        )


def _split(team: Team) -> SideState:
    state = SideState(team=team)
    for player in team.starters:
        actual = player.actual_points
        # None means the game has not been played. A real 0.0 from a completed
        # game is indistinguishable here, which is why `yet_to_play` leans on
        # None rather than on a falsy check.
        if actual is None:
            state.yet_to_play.append(player)
        else:
            state.scored += actual
            state.played.append(player)
    state.scored = round(state.scored, 1)
    return state


@dataclass
class LiveMatchup:
    mine: SideState
    theirs: SideState

    @property
    def margin(self) -> float:
        return round(self.mine.scored - self.theirs.scored, 1)

    @property
    def projected_margin(self) -> float:
        return round(self.mine.projected_final - self.theirs.projected_final, 1)

    def needed_from_remaining(self) -> float:
        """Points my remaining players need for me to end level."""
        return round(
            max(0.0, self.theirs.projected_final - self.mine.scored), 1
        )

    def describe(self) -> str:
        lines = [
            f"LIVE: you {self.mine.scored:.1f} — {self.theirs.scored:.1f} "
            f"{self.theirs.team.name} ({self.margin:+.1f})",
            "",
            "  " + self.mine.describe(),
            "  " + self.theirs.describe(),
            "",
            f"  Projected final: {self.mine.projected_final:.1f} — "
            f"{self.theirs.projected_final:.1f} ({self.projected_margin:+.1f})",
        ]
        if self.mine.yet_to_play:
            need = self.needed_from_remaining()
            names = " + ".join(p.name for p in self.mine.yet_to_play)
            lines.append(
                f"  You need roughly {need:.1f} combined from {names} to draw level."
            )
        elif self.theirs.yet_to_play:
            lines.append(
                f"  You are done. They need "
                f"{max(0.0, self.mine.scored - self.theirs.scored):.1f} "
                f"from {len(self.theirs.yet_to_play)} remaining player(s)."
            )
        return "\n".join(lines)


def live_matchup(league: League, my_team_id: int, week: int | None = None) -> LiveMatchup | None:
    matchup = league.matchup_for(my_team_id, week)
    if matchup is None:
        return None
    other_id = (
        matchup.away_team_id
        if matchup.home_team_id == my_team_id
        else matchup.home_team_id
    )
    mine = league.team(my_team_id)
    theirs = league.team(other_id) if other_id else None
    if mine is None or theirs is None:
        return None
    return LiveMatchup(mine=_split(mine), theirs=_split(theirs))
