"""
Deterministic lineup analysis.

Everything here is arithmetic and rule-checking with exact answers, so none of
it uses a model. The agent's job is the part that genuinely requires judgement
-- what a `QUESTIONABLE` tag really means this week, and whether any of this is
worth interrupting a human about.
"""

from __future__ import annotations

from espn.models import Player, Team


def lineup_problems(team: Team) -> list[dict]:
    """
    Starters who should not be starting.

    Rule-based on purpose. The agent's job later is the hard part -- deciding
    what to do about a `QUESTIONABLE` player, and whether it is worth
    interrupting you. This just establishes that the signal is there.
    """
    problems: list[dict] = []
    for player in team.starters:
        if player.is_out:
            problems.append(
                {
                    "player": player,
                    "severity": "out",
                    "detail": f"ruled {(player.injury_status or 'OUT').replace('_', ' ').lower()}",
                }
            )
        elif player.is_risky:
            problems.append(
                {
                    "player": player,
                    "severity": "risky",
                    "detail": (player.injury_status or "").lower(),
                }
            )
        elif player.projected_points is not None and player.projected_points <= 0:
            # A zero projection on a healthy starter usually means a bye week.
            problems.append(
                {"player": player, "severity": "zero", "detail": "projected 0.0 (bye?)"}
            )
    return problems


def bench_alternatives(team: Team, player: Player) -> list[Player]:
    """Healthy bench players who could legally take this player's slot."""
    return sorted(
        (
            p
            for p in team.bench
            if p.can_play(player.slot) and not p.is_out and (p.projected_points or 0) > 0
        ),
        key=lambda p: -(p.projected_points or 0),
    )[:3]


def bench_upgrades(team: Team, *, min_gain: float = 1.0) -> list[dict]:
    """
    Points sitting on your bench.

    For each starter, the best healthy bench player eligible for that slot who
    out-projects him. Pure arithmetic -- no model needed, and no model should
    be used, because the answer is exact.

    Each bench player is offered for only one swap, so a single strong backup
    does not get proposed as the fix for three different slots at once.
    """
    upgrades: list[dict] = []
    claimed: set[int] = set()

    candidates = sorted(
        (p for p in team.bench if not p.is_out and (p.projected_points or 0) > 0),
        key=lambda p: -(p.projected_points or 0),
    )

    for starter in sorted(team.starters, key=lambda p: (p.projected_points or 0)):
        best = next(
            (
                c
                for c in candidates
                if c.player_id not in claimed
                and c.can_play(starter.slot)
                and (c.projected_points or 0) - (starter.projected_points or 0) >= min_gain
            ),
            None,
        )
        if best is not None:
            claimed.add(best.player_id)
            upgrades.append(
                {
                    "out": starter,
                    "in": best,
                    "gain": round(
                        (best.projected_points or 0) - (starter.projected_points or 0), 1
                    ),
                }
            )

    return sorted(upgrades, key=lambda u: -u["gain"])
