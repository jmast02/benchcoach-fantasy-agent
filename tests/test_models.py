"""
Slot and position rules.

These maps are the quiet failure point: get one wrong and the code still runs,
the lineup still looks plausible, and it starts a kicker at flex.
"""

from espn.models import ELIGIBLE_SLOTS, STARTING_SLOTS, Player, Position, Slot, Team


def _p(pid, name, pos, slot, proj=10.0, injury=None):
    return Player(
        player_id=pid, name=name, position=pos, slot=slot,
        pro_team_id=1, injury_status=injury, projected_points=proj,
        eligible_slots=ELIGIBLE_SLOTS.get(pos, frozenset()),
    )


def test_bench_and_ir_do_not_score():
    assert Slot.BENCH not in STARTING_SLOTS
    assert Slot.IR not in STARTING_SLOTS
    assert Slot.FLEX in STARTING_SLOTS


def test_flex_accepts_rb_wr_te_but_not_qb_k_dst():
    assert _p(1, "a", Position.RB, Slot.BENCH).can_play(Slot.FLEX)
    assert _p(2, "b", Position.WR, Slot.BENCH).can_play(Slot.FLEX)
    assert _p(3, "c", Position.TE, Slot.BENCH).can_play(Slot.FLEX)
    assert not _p(4, "d", Position.QB, Slot.BENCH).can_play(Slot.FLEX)
    assert not _p(5, "e", Position.K, Slot.BENCH).can_play(Slot.FLEX)
    assert not _p(6, "f", Position.DST, Slot.BENCH).can_play(Slot.FLEX)


def test_a_kicker_cannot_be_slotted_anywhere_but_k():
    kicker = _p(7, "k", Position.K, Slot.BENCH)
    for slot in (Slot.QB, Slot.RB, Slot.WR, Slot.TE, Slot.FLEX, Slot.DST):
        assert not kicker.can_play(slot)
    assert kicker.can_play(Slot.K)


def test_injury_status_classification():
    assert _p(8, "x", Position.RB, Slot.RB, injury="OUT").is_out
    assert _p(9, "y", Position.RB, Slot.RB, injury="INJURY_RESERVE").is_out
    assert _p(10, "z", Position.RB, Slot.RB, injury="QUESTIONABLE").is_risky
    assert not _p(11, "w", Position.RB, Slot.RB, injury="ACTIVE").is_out
    assert not _p(12, "v", Position.RB, Slot.RB, injury=None).is_risky


def test_team_splits_starters_from_bench():
    team = Team(
        team_id=1, name="t", abbrev="T", owner=None,
        roster=[
            _p(1, "starter", Position.RB, Slot.RB),
            _p(2, "flexed", Position.WR, Slot.FLEX),
            _p(3, "benched", Position.TE, Slot.BENCH),
            _p(4, "injured", Position.WR, Slot.IR),
        ],
    )
    assert {p.name for p in team.starters} == {"starter", "flexed"}
    assert {p.name for p in team.bench} == {"benched"}
