"""Bench-upgrade arithmetic: the thing that catches free points."""

from analysis.optimizer import bench_upgrades
from espn.models import ELIGIBLE_SLOTS, Player, Position, Slot, Team


def _p(pid, name, pos, slot, proj, injury=None):
    return Player(
        player_id=pid, name=name, position=pos, slot=slot, pro_team_id=1,
        injury_status=injury, projected_points=proj,
        eligible_slots=ELIGIBLE_SLOTS.get(pos, frozenset()),
    )


def _team(*players):
    return Team(team_id=1, name="t", abbrev="T", owner=None, roster=list(players))


def test_finds_a_better_bench_player():
    team = _team(
        _p(1, "Mahomes", Position.QB, Slot.QB, 15.6),
        _p(2, "Lamar", Position.QB, Slot.BENCH, 19.9),
    )
    [upgrade] = bench_upgrades(team)
    assert upgrade["in"].name == "Lamar"
    assert upgrade["out"].name == "Mahomes"
    assert upgrade["gain"] == 4.3


def test_ignores_ineligible_bench_players():
    """A QB cannot replace a WR, however good his projection."""
    team = _team(
        _p(1, "wr", Position.WR, Slot.WR, 5.0),
        _p(2, "qb", Position.QB, Slot.BENCH, 25.0),
    )
    assert bench_upgrades(team) == []


def test_ignores_injured_and_zero_projection_bench_players():
    team = _team(
        _p(1, "starter", Position.RB, Slot.RB, 5.0),
        _p(2, "hurt", Position.RB, Slot.BENCH, 20.0, injury="OUT"),
        _p(3, "bye", Position.RB, Slot.BENCH, 0.0),
    )
    assert bench_upgrades(team) == []


def test_one_bench_player_is_not_proposed_for_two_slots():
    """A single strong backup must not be the 'fix' for several starters."""
    team = _team(
        _p(1, "wr1", Position.WR, Slot.WR, 4.0),
        _p(2, "wr2", Position.WR, Slot.WR, 5.0),
        _p(3, "stud", Position.WR, Slot.BENCH, 18.0),
    )
    upgrades = bench_upgrades(team)
    assert len(upgrades) == 1
    assert upgrades[0]["out"].name == "wr1"  # helps the weakest starter first


def test_small_gains_are_ignored():
    team = _team(
        _p(1, "a", Position.RB, Slot.RB, 10.0),
        _p(2, "b", Position.RB, Slot.BENCH, 10.5),
    )
    assert bench_upgrades(team) == []
