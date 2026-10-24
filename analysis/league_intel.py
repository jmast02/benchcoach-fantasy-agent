"""
League-wide competitive intelligence.

The differentiator. Almost every fantasy tool looks only at your team, because
cross-referencing ten rosters is exactly the tedium a human skips -- and it is
exactly what software should do instead.

Three questions it answers:

  Who else needs this player?   -> whether to spend now or wait
  How exposed is my opponent?   -> whether this week is already won or lost
  Who should I trade with?      -> whose surplus matches my hole

All deterministic. The agent's job is picking which of these is worth saying.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from espn.models import ELIGIBLE_SLOTS, League, Player, Position, Slot, Team

# Positions worth reasoning about for needs and trades. Kickers and defenses
# are streamed week to week rather than held, so a "deficit" there is noise.
CORE_POSITIONS = (Position.QB, Position.RB, Position.WR, Position.TE)


@dataclass
class PositionNeed:
    """
    How well stocked a team is at one position.

    "Need" deliberately means *no usable backup*, not *cannot field a lineup*.
    The stricter reading sounds right and is useless in practice: measured
    across a real 12-team league in week 7, literally no team was unable to
    fill a starting slot, so nothing ever fired. A manager starting two RBs
    with nobody behind them is one ankle away from trouble and will absolutely
    claim an RB -- that is the state worth detecting.
    """

    position: Position
    required: int          # starting slots at this position
    healthy: int           # rostered players who could actually play
    deficit: int           # starting slots that cannot be filled at all
    depth: int             # usable players beyond the starters

    @property
    def needs(self) -> bool:
        return self.depth <= 0

    @property
    def surplus(self) -> bool:
        return self.depth >= 2

    @property
    def label(self) -> str:
        if self.deficit > 0:
            return "critical"
        if self.needs:
            return "thin"
        if self.surplus:
            return "surplus"
        return "ok"


@dataclass
class TeamProfile:
    team: Team
    needs: dict[Position, PositionNeed] = field(default_factory=dict)
    faab_remaining: float | None = None
    waiver_rank: int | None = None

    def needs_at(self, position: Position) -> bool:
        need = self.needs.get(position)
        return bool(need and need.needs)

    def short_of(self) -> list[Position]:
        return [p for p, n in self.needs.items() if n.needs]

    def deep_at(self) -> list[Position]:
        return [p for p, n in self.needs.items() if n.surplus]


def _required_starters(league: League) -> dict[Position, int]:
    """
    How many of each position a legal lineup needs.

    FLEX is counted separately rather than assigned to a position, because a
    flex slot creates demand for RB/WR/TE collectively, not for any one of them.
    """
    required: dict[Position, int] = dict.fromkeys(CORE_POSITIONS, 0)
    for slot, count in league.roster_slots.items():
        for position in CORE_POSITIONS:
            if slot in ELIGIBLE_SLOTS.get(position, frozenset()) and slot not in (
                Slot.BENCH,
                Slot.IR,
                Slot.FLEX,
                Slot.RB_WR,
                Slot.WR_TE,
                Slot.OP,
            ):
                required[position] += count
    return required


def profile_team(team: Team, required: dict[Position, int]) -> TeamProfile:
    profile = TeamProfile(team=team, waiver_rank=team.waiver_rank)

    for position, need_count in required.items():
        healthy = sum(
            1
            for p in team.roster
            if p.position is position and not p.is_out and p.slot is not Slot.IR
        )
        profile.needs[position] = PositionNeed(
            position=position,
            required=need_count,
            healthy=healthy,
            deficit=max(0, need_count - healthy),
            depth=healthy - need_count,
        )
    return profile


def profile_league(league: League) -> dict[int, TeamProfile]:
    required = _required_starters(league)
    return {t.team_id: profile_team(t, required) for t in league.teams}


# ----------------------------------------------------------- waiver rivalry

@dataclass
class WaiverCompetition:
    position: Position
    rivals: list[TeamProfile]

    @property
    def contested(self) -> bool:
        return bool(self.rivals)

    def summary(self) -> str:
        if not self.rivals:
            return (
                f"Nobody else needs a {POSITION_LABEL[self.position]} right now — "
                "no rush, you can wait and see who gets dropped."
            )
        names = ", ".join(r.team.name for r in self.rivals[:3])
        more = f" and {len(self.rivals) - 3} others" if len(self.rivals) > 3 else ""
        return (
            f"{len(self.rivals)} other team(s) need a "
            f"{POSITION_LABEL[self.position]}: {names}{more}. "
            "Claim rather than wait."
        )


POSITION_LABEL = {
    Position.QB: "QB", Position.RB: "RB", Position.WR: "WR",
    Position.TE: "TE", Position.K: "K", Position.DST: "D/ST",
}


def waiver_competition(
    league: League, position: Position, *, exclude_team_id: int | None = None
) -> WaiverCompetition:
    """Which other teams have an unfilled starting slot at this position."""
    profiles = profile_league(league)
    rivals = [
        p
        for tid, p in profiles.items()
        if tid != exclude_team_id and p.needs_at(position)
    ]
    rivals.sort(key=lambda p: (p.waiver_rank if p.waiver_rank is not None else 99))
    return WaiverCompetition(position=position, rivals=rivals)


def blocking_opportunities(league: League, my_team_id: int) -> list[dict]:
    """
    Rivals who are short at a position -- the moves that hurt someone else.

    Entirely ordinary competitive play: holding a handcuff or claiming ahead of
    a rival is how waivers work, not an exploit.
    """
    profiles = profile_league(league)
    out: list[dict] = []
    for team_id, profile in profiles.items():
        if team_id == my_team_id:
            continue
        for position in profile.short_of():
            need = profile.needs[position]
            out.append(
                {
                    "team": profile.team,
                    "position": position,
                    "deficit": need.deficit,
                    "severity": need.label,
                    "waiver_rank": profile.waiver_rank,
                    "note": (
                        f"{profile.team.name} has no backup "
                        f"{POSITION_LABEL[position]}"
                        + (" and can't even fill the slot" if need.deficit else "")
                        + (
                            f" and holds waiver #{profile.waiver_rank}"
                            if profile.waiver_rank
                            else ""
                        )
                        + "."
                    ),
                }
            )
    out.sort(key=lambda d: (-d["deficit"], d["waiver_rank"] or 99))
    return out


# -------------------------------------------------------- opponent scouting

@dataclass
class OpponentReport:
    opponent: Team
    week: int
    out_starters: list[Player]
    zero_projected: list[Player]
    projected_total: float

    @property
    def exposure(self) -> int:
        return len(self.out_starters) + len(self.zero_projected)

    def summary(self) -> str:
        if not self.exposure:
            return f"{self.opponent.name} is at full strength."
        broken = ", ".join(
            p.name for p in (self.out_starters + self.zero_projected)[:3]
        )
        return (
            f"{self.opponent.name} has {self.exposure} compromised starter(s): "
            f"{broken}."
        )


def scout_opponent(
    league: League, my_team_id: int, week: int | None = None
) -> OpponentReport | None:
    """What is wrong with this week's opponent, and how much it is worth."""
    matchup = league.matchup_for(my_team_id, week)
    if matchup is None:
        return None

    other_id = (
        matchup.away_team_id
        if matchup.home_team_id == my_team_id
        else matchup.home_team_id
    )
    opponent = league.team(other_id) if other_id else None
    if opponent is None:
        return None

    starters = opponent.starters
    return OpponentReport(
        opponent=opponent,
        week=week or league.current_week,
        out_starters=[p for p in starters if p.is_out],
        zero_projected=[
            p
            for p in starters
            if not p.is_out
            and p.projected_points is not None
            and p.projected_points <= 0
        ],
        projected_total=round(sum(p.projected_points or 0 for p in starters), 1),
    )


# ------------------------------------------------------------ trade targets

@dataclass
class TradeMatch:
    partner: Team
    they_need: list[Position]
    they_can_spare: list[Position]
    overlap_score: int

    def summary(self) -> str:
        give = ", ".join(POSITION_LABEL[p] for p in self.they_need)
        get = ", ".join(POSITION_LABEL[p] for p in self.they_can_spare)
        return (
            f"{self.partner.name} is short at {give} and deep at {get} — "
            f"the mirror of your roster."
        )


def trade_matches(league: League, my_team_id: int) -> list[TradeMatch]:
    """
    Teams whose shape is the inverse of yours.

    A trade needs both halves: they have what you lack *and* lack what you
    have. Matching on only one side produces the offers everyone ignores.
    """
    profiles = profile_league(league)
    mine = profiles.get(my_team_id)
    if mine is None:
        return []

    my_needs = set(mine.short_of())
    my_depth = set(mine.deep_at())

    matches: list[TradeMatch] = []
    for team_id, profile in profiles.items():
        if team_id == my_team_id:
            continue
        they_need = set(profile.short_of()) & my_depth
        they_spare = set(profile.deep_at()) & my_needs
        if not (they_need and they_spare):
            continue
        matches.append(
            TradeMatch(
                partner=profile.team,
                they_need=sorted(they_need, key=lambda p: p.value),
                they_can_spare=sorted(they_spare, key=lambda p: p.value),
                overlap_score=len(they_need) + len(they_spare),
            )
        )
    matches.sort(key=lambda m: -m.overlap_score)
    return matches
