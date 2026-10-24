"""
Usage-trend maths.

No network: `_window` takes a frame and `direction()` takes Trend objects, so
the logic is testable from synthetic data.
"""

import polars as pl
import pytest

from stats.trends import WINDOW, PlayerTrend, Trend, _window


def _frame(weeks, values, column="offense_pct"):
    return pl.DataFrame({"week": weeks, column: values})


def _trend(metric, recent, prior, gr=3, gp=3):
    return Trend(metric=metric, recent=recent, prior=prior, games_recent=gr, games_prior=gp)


# ------------------------------------------------------------------ windows

def test_window_splits_recent_from_prior():
    frame = _frame([1, 2, 3, 4, 5, 6], [10, 10, 10, 20, 20, 20])
    trend = _window(frame, "offense_pct", 6)
    assert trend.recent == 20
    assert trend.prior == 10
    assert trend.delta == 10
    assert trend.games_recent == WINDOW and trend.games_prior == WINDOW


def test_window_respects_the_week_cutoff():
    """Later weeks must be invisible when analysing an earlier one."""
    frame = _frame([1, 2, 3, 4, 5, 6], [10, 10, 10, 99, 99, 99])
    trend = _window(frame, "offense_pct", 3)
    assert trend.recent == 10
    assert trend.games_recent == 3


def test_window_skips_missing_weeks_rather_than_zeroing_them():
    """A bye or an inactive week is absent, not a zero -- averaging in zeros
    would make every player who missed a game look like he lost his job."""
    frame = _frame([1, 4, 5, 6], [80, 80, 80, 80])
    trend = _window(frame, "offense_pct", 6)
    assert trend.recent == 80
    assert trend.games_recent == 3
    assert trend.games_prior == 1


def test_window_handles_no_history():
    assert _window(_frame([], []), "offense_pct", 6) is None
    assert _window(_frame([1], [50]), "missing_column", 6) is None


def test_window_needs_two_games_each_side_for_a_baseline():
    thin = _window(_frame([1, 2, 3, 4], [10, 10, 20, 20]), "offense_pct", 4)
    assert thin.games_prior == 1
    assert not thin.has_baseline

    solid = _window(_frame([1, 2, 3, 4, 5], [10, 10, 20, 20, 20]), "offense_pct", 5)
    assert solid.has_baseline


# --------------------------------------------------------------- direction

def _player(position="WR", snap=None, target=None, carries=None):
    return PlayerTrend(
        espn_id=1, gsis_id="g", name="Test Player", position=position,
        team="KC", snap_pct=snap, target_share=target, carries=carries,
    )


def test_rising_requires_both_signals_to_agree():
    agreeing = _player(
        snap=_trend("offense_pct", 86, 66),
        target=_trend("target_share", 0.25, 0.15),
    )
    assert agreeing.direction() == "rising"

    # Snaps up but role usage down: not a buy, whatever the snap count says.
    disagreeing = _player(
        snap=_trend("offense_pct", 86, 66),
        target=_trend("target_share", 0.10, 0.25),
    )
    assert disagreeing.direction() == "steady"


def test_fading_requires_both_signals_to_agree():
    player = _player(
        position="RB",
        snap=_trend("offense_pct", 38, 54),
        carries=_trend("carries", 10.0, 16.7),
    )
    assert player.direction() == "fading"


def test_small_moves_are_steady():
    player = _player(
        snap=_trend("offense_pct", 72, 71),
        target=_trend("target_share", 0.23, 0.27),
    )
    assert player.direction() == "steady"


def test_insufficient_history_is_thin_not_unknown():
    """`thin` and `n-a` mean different things and must stay distinguishable."""
    player = _player(snap=_trend("offense_pct", 90, 80, gr=3, gp=1))
    assert player.direction() == "thin"
    assert player.headline() is None


@pytest.mark.parametrize("position", ["K", "PK", "D/ST", "DST"])
def test_kickers_and_defenses_are_not_judged_on_snap_share(position):
    player = _player(position=position, snap=_trend("offense_pct", 0, 0))
    assert player.direction() == "n-a"


def test_headline_only_for_rising_or_fading():
    rising = _player(
        snap=_trend("offense_pct", 86, 66),
        target=_trend("target_share", 0.25, 0.15),
    )
    text = rising.headline()
    assert "trending up" in text
    assert "66% → 86%" in text
    assert "15% → 25%" in text

    steady = _player(
        snap=_trend("offense_pct", 72, 71),
        target=_trend("target_share", 0.23, 0.27),
    )
    assert steady.headline() is None


def test_usage_metric_follows_position():
    wr = _player(position="WR", target=_trend("target_share", 0.3, 0.2),
                 carries=_trend("carries", 1, 1))
    assert wr.usage.metric == "target_share"
    rb = _player(position="RB", target=_trend("target_share", 0.3, 0.2),
                 carries=_trend("carries", 15, 10))
    assert rb.usage.metric == "carries"
    assert _player(position="QB").usage is None
