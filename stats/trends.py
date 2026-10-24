"""
Usage trends: who is being used more, and who is being used less.

Production is noisy -- a receiver can score twice on three targets and look
like a breakout. Usage is far more stable and far more predictive, so the
signals here are snap share, target share and carries rather than fantasy
points. A player whose snap share is climbing is a buy even in a week he did
nothing with it.

All arithmetic, no model. The agent's job is to decide which of these is worth
saying out loud, not to compute them.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from stats.nflverse import (
    CURRENT_SEASON,
    crosswalk,
    last_complete_week,
    snap_counts,
    weekly_stats,
)

# Three games each side: long enough to damp a single outlier, short enough to
# notice a change in role within a few weeks of it happening.
WINDOW = 3


@dataclass
class Trend:
    """One metric, recent form against the period before it."""

    metric: str
    recent: float
    prior: float
    games_recent: int
    games_prior: int

    @property
    def delta(self) -> float:
        return round(self.recent - self.prior, 3)

    @property
    def has_baseline(self) -> bool:
        """Whether there is enough history for the comparison to mean anything."""
        return self.games_recent >= 2 and self.games_prior >= 2


@dataclass
class PlayerTrend:
    espn_id: int
    gsis_id: str | None
    name: str
    position: str | None
    team: str | None
    snap_pct: Trend | None = None
    target_share: Trend | None = None
    carries: Trend | None = None
    points: Trend | None = None

    @property
    def usage(self) -> Trend | None:
        """The usage signal that best fits this position."""
        if self.position in ("WR", "TE"):
            return self.target_share
        if self.position == "RB":
            return self.carries
        return None

    @property
    def games(self) -> int:
        """Games of usable history, from whichever metric has the most."""
        counts = [
            t.games_recent + t.games_prior
            for t in (self.snap_pct, self.target_share, self.carries, self.points)
            if t is not None
        ]
        return max(counts) if counts else 0

    def direction(self) -> str:
        """
        rising / fading / steady / thin / n-a.

        `thin` and `n-a` are reported separately rather than folded into a
        vague "unknown", because they mean different things and the agent
        should say different things about them: `thin` is a player without
        enough history to judge yet (a rookie, or someone returning from
        injury), while `n-a` is a position for which snap and target share
        carry no signal at all.

        Both available signals must agree before a call is made. Snap share
        alone rises for boring reasons -- garbage time, an injury elsewhere --
        and target share alone swings on a single bad game script.
        """
        if self.position in ("K", "PK", "DEF", "DST", "D/ST"):
            return "n-a"

        signals = []
        if self.snap_pct and self.snap_pct.has_baseline:
            signals.append(self.snap_pct.delta)
        usage = self.usage
        if usage and usage.has_baseline:
            # Normalise so the two sit on comparable scales.
            scale = 100.0 if usage.metric == "target_share" else 2.0
            signals.append(usage.delta * scale)

        if not signals:
            return "thin"
        if all(s >= 4 for s in signals) and max(signals) >= 8:
            return "rising"
        if all(s <= -4 for s in signals) and min(signals) <= -8:
            return "fading"
        return "steady"

    def headline(self) -> str | None:
        """One plain sentence, or None when there is nothing worth saying."""
        direction = self.direction()
        if direction not in ("rising", "fading"):
            return None

        parts = []
        if self.snap_pct and self.snap_pct.has_baseline:
            parts.append(
                f"snap share {self.snap_pct.prior:.0f}% → {self.snap_pct.recent:.0f}%"
            )
        usage = self.usage
        if usage and usage.has_baseline:
            if usage.metric == "target_share":
                parts.append(
                    f"target share {usage.prior * 100:.0f}% → {usage.recent * 100:.0f}%"
                )
            else:
                parts.append(f"carries {usage.prior:.1f} → {usage.recent:.1f}")

        verb = "trending up" if direction == "rising" else "losing work"
        return f"{self.name} is {verb}: " + ", ".join(parts) + "."


def _window(frame: pl.DataFrame, column: str, week: int) -> Trend | None:
    """Mean of `column` over the last WINDOW games played, vs the WINDOW before."""
    if column not in frame.columns or not frame.height:
        return None

    played = (
        frame.filter(pl.col("week") <= week)
        .filter(pl.col(column).is_not_null())
        .sort("week")
    )
    if not played.height:
        return None

    values = played[column].to_list()
    recent = values[-WINDOW:]
    prior = values[-(WINDOW * 2) : -WINDOW]
    if not recent:
        return None

    return Trend(
        metric=column,
        recent=round(sum(recent) / len(recent), 3),
        prior=round(sum(prior) / len(prior), 3) if prior else 0.0,
        games_recent=len(recent),
        games_prior=len(prior),
    )


def player_trend(
    espn_id: int, week: int | None = None, *, season: int = CURRENT_SEASON
) -> PlayerTrend | None:
    """
    Usage trends for one ESPN player, or None if we can't identify them.

    `week` defaults to the last *completed* week -- see
    `stats.nflverse.last_complete_week` for why a partially-played week must
    not be included.
    """
    if week is None:
        week = last_complete_week(season)
    identity = crosswalk().get(espn_id)
    if identity is None:
        return None

    gsis_id = identity.get("gsis_id")
    pfr_id = identity.get("pfr_id")
    trend = PlayerTrend(
        espn_id=espn_id,
        gsis_id=gsis_id,
        name=identity.get("name") or "",
        position=identity.get("position"),
        team=identity.get("team"),
    )

    if gsis_id:
        stats = weekly_stats(season).filter(pl.col("player_id") == gsis_id)
        trend.target_share = _window(stats, "target_share", week)
        trend.carries = _window(stats, "carries", week)
        trend.points = _window(stats, "fantasy_points_ppr", week)

    if pfr_id:
        snaps = snap_counts(season).filter(pl.col("pfr_player_id") == pfr_id)
        snap_trend = _window(snaps, "offense_pct", week)
        if snap_trend:
            # nflverse reports this as a fraction in some seasons and a
            # percentage in others; normalise to percent so thresholds hold.
            if max(snap_trend.recent, snap_trend.prior) <= 1.0:
                snap_trend.recent = round(snap_trend.recent * 100, 1)
                snap_trend.prior = round(snap_trend.prior * 100, 1)
            trend.snap_pct = snap_trend

    return trend


def roster_trends(
    espn_ids: list[int], week: int | None = None, *, season: int = CURRENT_SEASON
) -> dict[int, PlayerTrend]:
    if week is None:
        week = last_complete_week(season)
    out: dict[int, PlayerTrend] = {}
    for espn_id in espn_ids:
        trend = player_trend(espn_id, week, season=season)
        if trend is not None:
            out[espn_id] = trend
    return out
