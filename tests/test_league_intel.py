"""
League-wide reasoning.

The central judgement under test is what "need" means. Defining it as "cannot
field a legal lineup" sounds rigorous and detects nothing -- measured across a
real 12-team league in week 7, no team was ever unable to fill a slot. A team
starting two RBs with nobody behind them is the state that actually matters.
"""

from analysis.league_intel import (
    blocking_opportunities,
    profile_league,
    scout_opponent,
    trade_matches,
    waiver_competition,
)
from espn.models import ELIGIBLE_SLOTS, League, Matchup, Player, Position, Slot, Team


def _p(pid, pos, slot, proj=10.0, injury=None):
    return Player(
        player_id=pid, name=f"p{pid}", position=pos, slot=slot, pro_team_id=1,
        injury_status=injury, projected_points=proj,
        eligible_slots=ELIGIBLE_SLOTS.get(pos, frozenset()),
    )


def _team(tid, name, players, waiver=None):
    return Team(
        team_id=tid, name=name, abbrev=name[:4], owner=None,
        roster=players, waiver_rank=waiver,
    )


def _league(teams, matchups=None):
    return League(
        league_id=1, season=2026, name="L", current_week=7, teams=teams,
        matchups=matchups or [],
        roster_slots={Slot.QB: 1, Slot.RB: 2, Slot.WR: 2, Slot.TE: 1, Slot.BENCH: 5},
    )


def _roster(qb=1, rb=2, wr=2, te=1):
    """A roster with the given healthy count at each position."""
    out, pid = [], 0
    for pos, n, start in (
        (Position.QB, qb, Slot.QB), (Position.RB, rb, Slot.RB),
        (Position.WR, wr, Slot.WR), (Position.TE, te, Slot.TE),
    ):
        for i in range(n):
            pid += 1
            out.append(_p(pid, pos, start if i == 0 else Slot.BENCH))
    return out


def test_no_backup_counts_as_a_need():
    """Exactly enough starters and nobody behind them is a need."""
    league = _league([_team(1, "A", _roster(rb=2))])
    need = profile_league(league)[1].needs[Position.RB]
    assert need.healthy == 2 and need.required == 2
    assert need.depth == 0
    assert need.needs
    assert need.label == "thin"


def test_spare_depth_is_not_a_need():
    league = _league([_team(1, "A", _roster(rb=3))])
    need = profile_league(league)[1].needs[Position.RB]
    assert not need.needs
    assert need.label == "ok"


def test_two_spare_is_a_surplus():
    league = _league([_team(1, "A", _roster(rb=4))])
    need = profile_league(league)[1].needs[Position.RB]
    assert need.surplus and need.label == "surplus"


def test_unfillable_slot_is_critical():
    league = _league([_team(1, "A", _roster(rb=1))])
    need = profile_league(league)[1].needs[Position.RB]
    assert need.deficit == 1
    assert need.label == "critical"


def test_injured_players_do_not_count_as_healthy():
    players = _roster(rb=2)
    players[1].injury_status = "OUT"
    league = _league([_team(1, "A", players)])
    need = profile_league(league)[1].needs[Position.RB]
    assert need.healthy == 1
    assert need.deficit == 1


def test_blocking_lists_rivals_but_never_yourself():
    league = _league([
        _team(1, "Me", _roster(rb=4, wr=4)),
        _team(2, "Rival", _roster(rb=2), waiver=1),
    ])
    blocks = blocking_opportunities(league, my_team_id=1)
    assert blocks
    assert all(b["team"].team_id != 1 for b in blocks)
    assert any(b["position"] is Position.RB and "Rival" in b["note"] for b in blocks)


def test_waiver_competition_is_sorted_by_waiver_priority():
    league = _league([
        _team(1, "Me", _roster(rb=4)),
        _team(2, "Later", _roster(rb=2), waiver=7),
        _team(3, "Sooner", _roster(rb=2), waiver=2),
    ])
    rivals = waiver_competition(league, Position.RB, exclude_team_id=1).rivals
    assert [r.team.name for r in rivals] == ["Sooner", "Later"]


def test_uncontested_waiver_says_so():
    league = _league([
        _team(1, "Me", _roster(rb=4)),
        _team(2, "Stocked", _roster(rb=4)),
    ])
    competition = waiver_competition(league, Position.RB, exclude_team_id=1)
    assert not competition.contested
    assert "no rush" in competition.summary()


def test_trade_needs_both_halves_to_match():
    """A partner must want what you have *and* have what you want."""
    me = _team(1, "Me", _roster(rb=4, te=1))        # RB surplus, TE thin
    mirror = _team(2, "Mirror", _roster(rb=2, te=3))  # RB thin, TE surplus
    onesided = _team(3, "OneSided", _roster(rb=2, te=1))  # needs RB, no TE depth

    matches = trade_matches(_league([me, mirror, onesided]), my_team_id=1)
    assert [m.partner.name for m in matches] == ["Mirror"]
    assert Position.RB in matches[0].they_need
    assert Position.TE in matches[0].they_can_spare


def test_scout_opponent_finds_broken_starters():
    mine = _team(1, "Me", _roster())
    their_players = _roster()
    their_players[1].injury_status = "OUT"
    theirs = _team(2, "Them", their_players)
    league = _league(
        [mine, theirs],
        [Matchup(week=7, home_team_id=1, away_team_id=2)],
    )
    report = scout_opponent(league, my_team_id=1)
    assert report.opponent.name == "Them"
    assert report.exposure == 1
    assert "Them" in report.summary()


def test_scout_opponent_handles_a_bye_week():
    league = _league([_team(1, "Me", _roster())], [])
    assert scout_opponent(league, my_team_id=1) is None
