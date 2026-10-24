"""
Tools the agent can call.

All reads are free and deterministic. The only tools with consequences are
`propose_lineup_swap` and `send_insight`, and neither executes anything -- the
first records a proposal that still needs human approval, the second sends a
message. The agent can never reach ESPN's write host.

Tool results are returned as plain readable text rather than JSON. The model
reads them, and a sentence like "snap share 65% -> 50% over 3 games" carries
the shape of the data better than a nested object.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from anthropic import beta_tool

from analysis.league_intel import (
    POSITION_LABEL,
    blocking_opportunities,
    profile_league,
    scout_opponent,
    waiver_competition,
)
from analysis.optimizer import bench_upgrades
from analysis.schedule import defense_schedule_outlook, offense_strength
from analysis.startsit import matchup_strategy, outlook
from approvals.models import LineupSwap
from espn.models import DEDICATED_SLOT, League, Position, Team
from stats.nflverse import injury_detail

logger = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """Everything the tools need, assembled once per run."""

    league: League
    team: Team
    client: object                 # ESPNClient, for lazy free-agent lookups
    approvals: object              # ApprovalService
    week: int
    dry_run: bool = True
    trends: dict = field(default_factory=dict)
    proposals: list = field(default_factory=list)
    insights: list = field(default_factory=list)
    max_messages: int = 3


_ctx: AgentContext | None = None


def set_context(ctx: AgentContext) -> None:
    global _ctx
    _ctx = ctx


def _require() -> AgentContext:
    if _ctx is None:
        raise RuntimeError("agent context not set")
    return _ctx


def _find_player(name: str):
    ctx = _require()
    needle = name.strip().lower()
    for player in ctx.team.roster:
        if needle in player.name.lower():
            return player
    return None


# --------------------------------------------------------------------- reads

@beta_tool
def get_my_roster() -> str:
    """Your team's current lineup and bench, with projections and injury tags.

    Start here. Shows who is starting, who is benched, each player's projected
    points for this week, and any injury designation.
    """
    ctx = _require()
    lines = [f"{ctx.team.name} ({ctx.team.record}), week {ctx.league.current_week}", ""]
    lines.append("STARTING:")
    for p in sorted(ctx.team.starters, key=lambda p: p.slot):
        tag = f"  [{p.injury_status}]" if p.injury_status and p.injury_status != "ACTIVE" else ""
        lines.append(
            f"  {p.slot_name:5} {p.position_name:5} {p.name:24} "
            f"proj {p.projected_points or 0:.1f}{tag}"
        )
    lines.append("")
    lines.append("BENCH:")
    for p in ctx.team.bench:
        tag = f"  [{p.injury_status}]" if p.injury_status and p.injury_status != "ACTIVE" else ""
        lines.append(
            f"  {'BE':5} {p.position_name:5} {p.name:24} "
            f"proj {p.projected_points or 0:.1f}{tag}"
        )
    return "\n".join(lines)


@beta_tool
def get_player_detail(player_name: str) -> str:
    """Usage trend and injury detail for one player on your roster.

    Includes snap share and role usage over recent weeks, plus the official
    injury report with practice participation -- which distinguishes a
    "questionable" player who practised fully from one who did not practise
    at all.

    Args:
        player_name: Full or partial name, e.g. "Swift".
    """
    ctx = _require()
    player = _find_player(player_name)
    if player is None:
        return f"No player matching {player_name!r} on your roster."

    out = [
        f"{player.name} ({player.position_name}) — "
        f"{'starting at ' + player.slot_name if player.is_starting else 'on the bench'}",
        f"projected this week: {player.projected_points or 0:.1f}",
        f"ESPN injury tag: {player.injury_status or 'none'}",
    ]

    trend = ctx.trends.get(player.player_id)
    if trend is None:
        out.append("usage data: not available (defenses and some players aren't tracked)")
    else:
        out.append(f"usage direction: {trend.direction()} ({trend.games} games of data)")
        if trend.snap_pct and trend.snap_pct.has_baseline:
            out.append(
                f"  snap share: {trend.snap_pct.prior:.0f}% -> "
                f"{trend.snap_pct.recent:.0f}%"
            )
        usage = trend.usage
        if usage and usage.has_baseline:
            if usage.metric == "target_share":
                out.append(
                    f"  target share: {usage.prior * 100:.0f}% -> {usage.recent * 100:.0f}%"
                )
            else:
                out.append(f"  carries: {usage.prior:.1f} -> {usage.recent:.1f}")
        if trend.direction() == "thin":
            out.append("  (too few games to read a trend — do not infer one)")

        detail = injury_detail(trend.gsis_id, ctx.week)
        if detail:
            out.append(
                f"injury report wk{detail.get('week')}: "
                f"status={detail.get('report_status') or 'none'}, "
                f"practice={detail.get('practice_status') or 'unknown'}, "
                f"injury={detail.get('report_primary_injury') or 'unspecified'}"
            )
        else:
            out.append("injury report: no entry (usually means healthy)")
    return "\n".join(out)


@beta_tool
def get_bench_upgrades() -> str:
    """Bench players who out-project a current starter they could replace.

    Pure projection arithmetic, already filtered for position eligibility,
    injuries and byes.
    """
    ctx = _require()
    upgrades = bench_upgrades(ctx.team)
    if not upgrades:
        return "No bench player out-projects a starter they're eligible to replace."
    return "\n".join(
        f"{u['in'].name} ({u['in'].projected_points:.1f}) over "
        f"{u['out'].name} ({u['out'].projected_points:.1f}) at "
        f"{u['out'].slot_name} — gain {u['gain']:+.1f}"
        for u in upgrades
    )


@beta_tool
def get_available_players(position: str, limit: int = 8) -> str:
    """Players available on waivers or free agency at a position.

    Args:
        position: One of QB, RB, WR, TE, K, DST.
        limit: How many to return (default 8).
    """
    ctx = _require()
    try:
        pos = {
            "QB": Position.QB, "RB": Position.RB, "WR": Position.WR,
            "TE": Position.TE, "K": Position.K, "DST": Position.DST,
            "D/ST": Position.DST,
        }[position.upper().strip()]
    except KeyError:
        return f"Unknown position {position!r}. Use QB, RB, WR, TE, K or DST."

    if pos not in DEDICATED_SLOT:
        return f"Cannot search for {position}."

    try:
        players = ctx.client.free_agents(
            positions=(pos,), limit=max(1, min(limit, 25)), week=ctx.league.current_week
        )
    except Exception as exc:
        return f"Could not load available players: {exc}"

    if not players:
        return f"No available {POSITION_LABEL[pos]}s found."
    lines = []
    for p in players:
        own = (
            f"{p.percent_owned:.0f}% owned"
            if p.percent_owned is not None
            else "ownership unknown"
        )
        tag = f" [{p.injury_status}]" if p.injury_status and p.injury_status != "ACTIVE" else ""
        lines.append(f"  {p.name:24} proj {p.projected_points or 0:.1f}  {own}{tag}")
    return f"Available {POSITION_LABEL[pos]}s:\n" + "\n".join(lines)


@beta_tool
def get_league_situation() -> str:
    """Your matchup this week, your roster's shape, and what rivals are short of.

    Useful for deciding urgency: whether a claim is contested, and whether this
    week is already comfortable.
    """
    ctx = _require()
    out: list[str] = []

    report = scout_opponent(ctx.league, ctx.team.team_id)
    if report:
        out.append(
            f"This week vs {report.opponent.name} ({report.opponent.record}), "
            f"their projected total {report.projected_total}."
        )
        out.append("  " + report.summary())

    profiles = profile_league(ctx.league)
    mine = profiles.get(ctx.team.team_id)
    if mine:
        short = [POSITION_LABEL[p] for p in mine.short_of()]
        deep = [POSITION_LABEL[p] for p in mine.deep_at()]
        shape = (
            "no backup at " + ", ".join(short)
            if short
            else "adequate depth everywhere"
        )
        if deep:
            shape += f"; surplus at {', '.join(deep)}"
        out.append(f"Your roster: {shape}")

    blocks = blocking_opportunities(ctx.league, ctx.team.team_id)[:4]
    if blocks:
        out.append("Rivals with no backup:")
        out.extend(f"  {b['note']}" for b in blocks)
    return "\n".join(out) or "No league information available."


@beta_tool
def get_waiver_competition(position: str) -> str:
    """How many rivals also need this position, and their waiver priority.

    Tells you whether to claim now or wait.

    Args:
        position: One of QB, RB, WR, TE.
    """
    ctx = _require()
    try:
        pos = {"QB": Position.QB, "RB": Position.RB,
               "WR": Position.WR, "TE": Position.TE}[position.upper().strip()]
    except KeyError:
        return f"Unknown position {position!r}."
    return waiver_competition(
        ctx.league, pos, exclude_team_id=ctx.team.team_id
    ).summary()


@beta_tool
def get_schedule_outlook(team_or_defense: str, weeks: int = 3) -> str:
    """Upcoming opponents for an NFL team, with how many points those opponents score.

    Use this for streaming decisions -- a defense facing weak offenses in
    coming weeks is worth acquiring before someone else does.

    Args:
        team_or_defense: Team name, nickname or abbreviation, e.g. "Ravens",
            "Ravens D/ST" or "BAL".
        weeks: How many upcoming games to show (default 3).
    """
    outlook = defense_schedule_outlook(team_or_defense, count=max(1, min(weeks, 6)))
    if outlook is None:
        return f"No schedule found for {team_or_defense!r}."
    strength = offense_strength()
    ranked = sorted(strength.items(), key=lambda kv: kv[1])
    weakest = ", ".join(f"{t} ({p})" for t, p in ranked[:5])
    return (
        f"{outlook.team} next {len(outlook.games)}: {outlook.describe()}\n"
        f"Average opponent scoring: {outlook.average_opponent_ppg} ppg "
        f"(league's weakest offenses: {weakest})"
    )


@beta_tool
def analyze_player(player_name: str) -> str:
    """Everything about a player's week that his projection does not capture.

    Opponent and how that defense performs against his position, the Vegas-
    implied game script, his floor and ceiling this season, and his usage
    trend. Use this whenever a start-sit decision is closer than the
    projections suggest, or when a projection looks wrong.

    Args:
        player_name: Full or partial name of a player on your roster.
    """
    ctx = _require()
    player = _find_player(player_name)
    if player is None:
        return f"No player matching {player_name!r} on your roster."
    return outlook(
        player, week=ctx.league.current_week, trends=ctx.trends
    ).describe()


@beta_tool
def compare_start_sit(player_a: str, player_b: str) -> str:
    """Compare two of your players side by side, beyond their projections.

    Returns the evidence for both -- matchup, script, floor and ceiling, usage
    trend -- plus whether this week calls for safety or upside. The decision is
    yours to make from it; this does not pick.

    Args:
        player_a: First player's name.
        player_b: Second player's name.
    """
    ctx = _require()
    first, second = _find_player(player_a), _find_player(player_b)
    if first is None:
        return f"No player matching {player_a!r}."
    if second is None:
        return f"No player matching {player_b!r}."

    week = ctx.league.current_week
    blocks = [
        outlook(first, week=week, trends=ctx.trends).describe(),
        outlook(second, week=week, trends=ctx.trends).describe(),
    ]

    report = scout_opponent(ctx.league, ctx.team.team_id)
    if report:
        strategy = matchup_strategy(ctx.team, report.opponent)
        blocks.append(f"This week's stance: {strategy.stance} — {strategy.advice()}")
    return "\n\n".join(blocks)


@beta_tool
def get_matchup_strategy() -> str:
    """Whether this week calls for the safe lineup or the high-variance one.

    Compares your projected total against your opponent's. A large lead means
    variance is your enemy; a large deficit means the safe lineup loses slowly
    and you need ceiling.
    """
    ctx = _require()
    report = scout_opponent(ctx.league, ctx.team.team_id)
    if report is None:
        return "No matchup found for this week (bye?)."
    strategy = matchup_strategy(ctx.team, report.opponent)
    return (
        f"You {strategy.my_total} vs {report.opponent.name} {strategy.their_total} "
        f"(margin {strategy.margin:+.1f}, stance: {strategy.stance})\n"
        f"{strategy.advice()}\n"
        f"{report.summary()}"
    )


@beta_tool
def get_opponent_lineup() -> str:
    """Your opponent's starting lineup this week, with projections and injuries.

    Use it to see where they are weak, where the game is likely decided, and
    whether their projected total is propped up by someone who may not play.
    """
    ctx = _require()
    report = scout_opponent(ctx.league, ctx.team.team_id)
    if report is None:
        return "No matchup this week."
    opponent = report.opponent
    lines = [f"{opponent.name} ({opponent.record}) — projected {report.projected_total}", ""]
    for p in sorted(opponent.starters, key=lambda p: p.slot):
        tag = f"  [{p.injury_status}]" if p.injury_status and p.injury_status != "ACTIVE" else ""
        lines.append(
            f"  {p.slot_name:5} {p.position_name:5} {p.name:24} "
            f"proj {p.projected_points or 0:.1f}{tag}"
        )
    if report.exposure:
        lines.append("")
        lines.append(report.summary())
    return "\n".join(lines)


# ------------------------------------------------------------------- actions

@beta_tool
def propose_lineup_swap(bench_player: str, starting_player: str, reason: str) -> str:
    """Propose starting a bench player in place of a current starter.

    This does NOT change anything. It records a proposal and sends it to the
    user for approval; only they can apply it. Use sparingly and only when the
    case is clear.

    Args:
        bench_player: Name of the bench player to start.
        starting_player: Name of the starter to bench.
        reason: One or two sentences, in plain language, citing the specific
            numbers that justify it.
    """
    ctx = _require()
    if len(ctx.proposals) + len(ctx.insights) >= ctx.max_messages:
        return (
            "Message limit reached for this run. Nothing sent — if this is more "
            "important than what you already sent, say so in your summary."
        )

    incoming = _find_player(bench_player)
    outgoing = _find_player(starting_player)
    if incoming is None:
        return f"No bench player matching {bench_player!r}."
    if outgoing is None:
        return f"No starter matching {starting_player!r}."
    if not outgoing.is_starting:
        return f"{outgoing.name} isn't currently starting."
    if not incoming.can_play(outgoing.slot):
        return (
            f"{incoming.name} ({incoming.position_name}) is not eligible for the "
            f"{outgoing.slot_name} slot. Proposal rejected."
        )

    swap = LineupSwap(
        out_player_id=outgoing.player_id, out_name=outgoing.name,
        out_slot=int(outgoing.slot), out_projected=outgoing.projected_points or 0.0,
        in_player_id=incoming.player_id, in_name=incoming.name,
        in_slot=int(incoming.slot), in_projected=incoming.projected_points or 0.0,
    )
    action = ctx.approvals.propose_swap(
        swap,
        reason=reason,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=90),
    )
    if action is None:
        return "An identical proposal is already awaiting their answer. Not sent again."

    ctx.proposals.append(action)
    return f"Proposal sent for approval (code {action.code}). They decide whether to apply it."


@beta_tool
def send_insight(message: str) -> str:
    """Send the user a short note worth knowing but not requiring a decision.

    Use rarely. Most observations do not deserve an interruption.

    Args:
        message: The text to send. One or two sentences, specific, no preamble.
    """
    ctx = _require()
    if len(ctx.proposals) + len(ctx.insights) >= ctx.max_messages:
        return "Message limit reached for this run. Not sent."
    try:
        ctx.approvals.channel.send(message)
    except Exception as exc:
        return f"Could not send: {exc}"
    ctx.insights.append(message)
    return "Sent."


@beta_tool
def send_digest(message: str) -> str:
    """Send the weekly digest. Only available on digest runs.

    Unlike `send_insight`, this is expected to always be used and is not
    subject to the per-run message limit -- the digest is a scheduled
    touchpoint the user has asked for, not an interruption.

    Args:
        message: The digest. A few short sections, specific numbers, no
            preamble. Written to be read on a phone.
    """
    ctx = _require()
    try:
        ctx.approvals.channel.send(message)
    except Exception as exc:
        return f"Could not send: {exc}"
    ctx.insights.append(message)
    return "Digest sent."


READ_TOOLS = [
    get_my_roster,
    get_player_detail,
    get_bench_upgrades,
    analyze_player,
    compare_start_sit,
    get_matchup_strategy,
    get_opponent_lineup,
    get_available_players,
    get_league_situation,
    get_waiver_competition,
    get_schedule_outlook,
]
ACTION_TOOLS = [propose_lineup_swap, send_insight]
ALL_TOOLS = READ_TOOLS + ACTION_TOOLS

# The digest swaps send_insight for send_digest: a digest run is a single
# scheduled message, not a judgement call about whether to interrupt.
DIGEST_TOOLS = READ_TOOLS + [send_digest]
