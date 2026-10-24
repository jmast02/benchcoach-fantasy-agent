"""
Playoff race, remaining schedule, and who you are actually competing with.

The league endpoint carries the full season schedule, not just played weeks,
which makes a surprising amount computable: who still plays whom, whose
remaining opponents are weak, and which teams are genuinely racing you for the
last playoff spot rather than merely sitting near you in the table.

This is what turns generic advice into leverage. A team that is mathematically
out will trade a good player for a bench flier; a team one game behind you will
not. Knowing which is which is the difference between an offer accepted and an
offer ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from espn.models import League, Team


@dataclass
class TeamRace:
    team: Team
    rank: int
    games_back: float                 # from the playoff cut line
    games_remaining: int
    remaining_opponents: list[Team] = field(default_factory=list)
    remaining_sos: float = 0.0        # average opponent points-per-game
    plays_me_again: list[int] = field(default_factory=list)

    @property
    def status(self) -> str:
        """
        Where this team sits in the race.

        Matters because it predicts behaviour: `selling` teams trade cheaply,
        `desperate` teams overpay for immediate help, and `safe` teams do
        neither.
        """
        if self.games_remaining == 0:
            return "done"
        if self.games_back <= -2:
            return "safe"
        if self.games_back <= 0:
            return "in"
        if self.games_back <= 1.5:
            return "bubble"
        if self.games_back > self.games_remaining:
            return "eliminated"
        if self.games_back >= self.games_remaining * 0.6:
            return "selling"
        return "desperate"

    def describe(self) -> str:
        where = (
            f"{abs(self.games_back):.1f} clear"
            if self.games_back < 0
            else f"{self.games_back:.1f} back"
        )
        note = f"{self.team.name} ({self.team.record}, #{self.rank}, {where}) — {self.status}"
        if self.plays_me_again:
            weeks = ", ".join(f"wk{w}" for w in self.plays_me_again)
            note += f"; you play them again {weeks}"
        return note


def _points_per_game(team: Team, played: int) -> float:
    return round(team.points_for / played, 1) if played else 0.0


def playoff_picture(league: League, my_team_id: int | None = None) -> list[TeamRace]:
    """
    The race, from each team's point of view.

    Games back is measured from the last playoff spot, so a negative number
    means a team is that far clear of the cut rather than behind it.
    """
    played = max(1, league.current_week - 1)

    ordered = sorted(
        league.teams, key=lambda t: (-t.wins, -t.points_for)
    )
    cut_index = min(league.playoff_teams, len(ordered)) - 1
    cut_team = ordered[cut_index] if ordered else None
    cut_wins = cut_team.wins if cut_team else 0

    ppg = {t.team_id: _points_per_game(t, played) for t in league.teams}

    races: list[TeamRace] = []
    for rank, team in enumerate(ordered, start=1):
        future = [m for m in league.matchups if m.week >= league.current_week]
        mine = [
            m for m in future
            if team.team_id in (m.home_team_id, m.away_team_id)
        ]
        opponents: list[Team] = []
        for match in mine:
            other_id = (
                match.away_team_id
                if match.home_team_id == team.team_id
                else match.home_team_id
            )
            other = league.team(other_id) if other_id else None
            if other:
                opponents.append(other)

        sos = (
            round(sum(ppg.get(o.team_id, 0.0) for o in opponents) / len(opponents), 1)
            if opponents
            else 0.0
        )

        rematches = []
        if my_team_id is not None and team.team_id != my_team_id:
            rematches = sorted(
                m.week for m in mine
                if my_team_id in (m.home_team_id, m.away_team_id)
            )

        races.append(
            TeamRace(
                team=team,
                rank=rank,
                games_back=float(cut_wins - team.wins),
                games_remaining=len(mine),
                remaining_opponents=opponents,
                remaining_sos=sos,
                plays_me_again=rematches,
            )
        )
    return races


def my_rivals(league: League, my_team_id: int) -> list[TeamRace]:
    """
    Teams genuinely racing you for a playoff spot.

    Within a game either way of your own position, excluding teams already
    safe or already eliminated -- those are not competing with you, whatever
    the table says.
    """
    races = playoff_picture(league, my_team_id)
    mine = next((r for r in races if r.team.team_id == my_team_id), None)
    if mine is None:
        return []
    return [
        r
        for r in races
        if r.team.team_id != my_team_id
        and abs(r.games_back - mine.games_back) <= 1.0
        and r.status not in ("safe", "eliminated", "done")
    ]


def trade_leverage(league: League, my_team_id: int) -> list[dict]:
    """
    Who will actually deal, and on what terms.

    Combines where a team sits in the race with how long their season has left.
    A selling team wants anything with future value; a desperate team overpays
    for immediate help; a direct rival should not be helped at all.
    """
    races = playoff_picture(league, my_team_id)
    rivals = {r.team.team_id for r in my_rivals(league, my_team_id)}

    out: list[dict] = []
    for race in races:
        if race.team.team_id == my_team_id:
            continue
        if race.team.team_id in rivals:
            posture = "do not help — directly racing you"
        elif race.status in ("eliminated", "selling"):
            posture = "likely seller — will take less than market"
        elif race.status == "desperate":
            posture = "will overpay for immediate help"
        elif race.status == "safe":
            posture = "comfortable — only moves for clear upgrades"
        else:
            posture = "neutral"
        out.append(
            {
                "team": race.team,
                "status": race.status,
                "rank": race.rank,
                "remaining_sos": race.remaining_sos,
                "posture": posture,
                "plays_me_again": race.plays_me_again,
            }
        )
    order = {
        "likely seller — will take less than market": 0,
        "will overpay for immediate help": 1,
        "neutral": 2,
        "comfortable — only moves for clear upgrades": 3,
        "do not help — directly racing you": 4,
    }
    out.sort(key=lambda d: order.get(d["posture"], 9))
    return out
