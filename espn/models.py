"""
Types for ESPN fantasy data, and the ID maps its API speaks in.

ESPN's API returns integers everywhere -- a player is not "WR on the bench",
he is `defaultPositionId: 3, lineupSlotId: 20`. Getting these maps wrong fails
quietly: the code runs, the lineup looks plausible, and it starts a kicker at
flex. They are defined once here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Slot(IntEnum):
    """`lineupSlotId` -- where a player currently sits."""

    QB = 0
    TQB = 1
    RB = 2
    RB_WR = 3
    WR = 4
    WR_TE = 5
    TE = 6
    OP = 7
    DT = 8
    DE = 9
    LB = 10
    DL = 11
    CB = 12
    S = 13
    DB = 14
    DP = 15
    DST = 16
    K = 17
    P = 18
    HC = 19
    BENCH = 20
    IR = 21
    FLEX = 23
    EDR = 24


# Slots that score. Everything else is storage.
STARTING_SLOTS = frozenset(
    {Slot.QB, Slot.TQB, Slot.RB, Slot.RB_WR, Slot.WR, Slot.WR_TE, Slot.TE,
     Slot.OP, Slot.DT, Slot.DE, Slot.LB, Slot.DL, Slot.CB, Slot.S, Slot.DB,
     Slot.DP, Slot.DST, Slot.K, Slot.P, Slot.HC, Slot.FLEX, Slot.EDR}
)

SLOT_NAMES = {
    Slot.QB: "QB", Slot.TQB: "TQB", Slot.RB: "RB", Slot.RB_WR: "RB/WR",
    Slot.WR: "WR", Slot.WR_TE: "WR/TE", Slot.TE: "TE", Slot.OP: "OP",
    Slot.DST: "D/ST", Slot.K: "K", Slot.BENCH: "BE", Slot.IR: "IR",
    Slot.FLEX: "FLEX", Slot.DT: "DT", Slot.DE: "DE", Slot.LB: "LB",
    Slot.DL: "DL", Slot.CB: "CB", Slot.S: "S", Slot.DB: "DB", Slot.DP: "DP",
    Slot.P: "P", Slot.HC: "HC", Slot.EDR: "EDR",
}


class Position(IntEnum):
    """`defaultPositionId` -- what a player actually is."""

    QB = 1
    RB = 2
    WR = 3
    TE = 4
    K = 5
    DST = 16


POSITION_NAMES = {
    Position.QB: "QB", Position.RB: "RB", Position.WR: "WR",
    Position.TE: "TE", Position.K: "K", Position.DST: "D/ST",
}

# Which slots a given position may legally occupy. Used by the optimizer, and
# the reason a swap can be rejected before it is ever proposed.
ELIGIBLE_SLOTS: dict[Position, frozenset[Slot]] = {
    Position.QB: frozenset({Slot.QB, Slot.TQB, Slot.OP, Slot.BENCH, Slot.IR}),
    Position.RB: frozenset({Slot.RB, Slot.RB_WR, Slot.FLEX, Slot.BENCH, Slot.IR}),
    Position.WR: frozenset({Slot.WR, Slot.RB_WR, Slot.WR_TE, Slot.FLEX, Slot.BENCH, Slot.IR}),
    Position.TE: frozenset({Slot.TE, Slot.WR_TE, Slot.FLEX, Slot.BENCH, Slot.IR}),
    Position.K: frozenset({Slot.K, Slot.BENCH, Slot.IR}),
    Position.DST: frozenset({Slot.DST, Slot.BENCH, Slot.IR}),
}

# The one slot a position exclusively owns. Used to filter the free-agent
# pool: filtering on *eligible* slots is far too broad, because RB eligibility
# includes FLEX, and asking for RBs then returns every WR and TE as well.
DEDICATED_SLOT: dict[Position, Slot] = {
    Position.QB: Slot.QB,
    Position.RB: Slot.RB,
    Position.WR: Slot.WR,
    Position.TE: Slot.TE,
    Position.K: Slot.K,
    Position.DST: Slot.DST,
}

# ESPN sends injury status as a string on the player object.
INJURY_OUT = frozenset({"OUT", "INJURY_RESERVE", "SUSPENSION", "NOT_ACTIVE"})
INJURY_RISKY = frozenset({"QUESTIONABLE", "DOUBTFUL"})


@dataclass
class Player:
    player_id: int
    name: str
    position: Position | None
    slot: Slot
    pro_team_id: int
    injury_status: str | None = None
    projected_points: float | None = None
    actual_points: float | None = None
    percent_owned: float | None = None
    eligible_slots: frozenset[Slot] = field(default_factory=frozenset)

    @property
    def position_name(self) -> str:
        return POSITION_NAMES.get(self.position, "?") if self.position else "?"

    @property
    def slot_name(self) -> str:
        return SLOT_NAMES.get(self.slot, str(int(self.slot)))

    @property
    def is_starting(self) -> bool:
        return self.slot in STARTING_SLOTS

    @property
    def is_out(self) -> bool:
        return (self.injury_status or "").upper() in INJURY_OUT

    @property
    def is_risky(self) -> bool:
        return (self.injury_status or "").upper() in INJURY_RISKY

    def can_play(self, slot: Slot) -> bool:
        """Whether this player may legally occupy `slot`."""
        if self.eligible_slots:
            return slot in self.eligible_slots
        if self.position is None:
            return False
        return slot in ELIGIBLE_SLOTS.get(self.position, frozenset())


@dataclass
class Team:
    team_id: int
    name: str
    abbrev: str
    owner: str | None
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    roster: list[Player] = field(default_factory=list)
    # Waiver/FAAB state -- needed to judge whether a rival can outbid you.
    acquisition_budget_spent: float | None = None
    waiver_rank: int | None = None

    @property
    def record(self) -> str:
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base

    @property
    def starters(self) -> list[Player]:
        return [p for p in self.roster if p.is_starting]

    @property
    def bench(self) -> list[Player]:
        return [p for p in self.roster if p.slot == Slot.BENCH]


@dataclass
class Matchup:
    week: int
    home_team_id: int
    away_team_id: int | None
    home_score: float = 0.0
    away_score: float = 0.0
    home_projected: float = 0.0
    away_projected: float = 0.0


@dataclass
class League:
    league_id: int
    season: int
    name: str
    current_week: int
    teams: list[Team] = field(default_factory=list)
    matchups: list[Matchup] = field(default_factory=list)
    # Lineup shape, e.g. {Slot.QB: 1, Slot.RB: 2, Slot.FLEX: 1}. Drives the
    # optimizer -- leagues differ and this cannot be assumed.
    roster_slots: dict[Slot, int] = field(default_factory=dict)

    def team(self, team_id: int) -> Team | None:
        return next((t for t in self.teams if t.team_id == team_id), None)

    def matchup_for(self, team_id: int, week: int | None = None) -> Matchup | None:
        target = week or self.current_week
        return next(
            (
                m for m in self.matchups
                if m.week == target and team_id in (m.home_team_id, m.away_team_id)
            ),
            None,
        )
