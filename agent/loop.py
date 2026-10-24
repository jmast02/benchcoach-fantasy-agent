"""
The agent run.

Assembles context, hands the model a set of read-only tools plus two
consequential ones, and lets it decide what -- if anything -- is worth telling
the user. The approval gate still sits between every proposal and any change to
the lineup; the agent cannot reach ESPN's write host.

Deliberately *not* agentic about everything. The lineup arithmetic is already
exact, so the model is not asked to recompute it; it is asked to judge the
ambiguous parts and to decide what deserves an interruption.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from agent.prompts import SYSTEM
from agent.tools import ALL_TOOLS, AgentContext, set_context
from approvals.channels import build_channel
from approvals.service import ApprovalService
from approvals.store import ActionStore
from config import get_settings
from espn.client import ESPNClient
from stats.nflverse import last_complete_week
from stats.trends import roster_trends

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    summary: str
    proposals: list
    insights: list
    tool_calls: list[str]
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def messages_sent(self) -> int:
        return len(self.proposals) + len(self.insights)

    def cost_estimate(self) -> float:
        """Rough USD, at Opus list pricing. Shown so the cost stays visible."""
        return round(self.input_tokens / 1e6 * 5 + self.output_tokens / 1e6 * 25, 4)


def build_context(*, max_messages: int = 3) -> tuple[AgentContext, ESPNClient]:
    settings = get_settings()
    client = ESPNClient(
        settings.league_id, settings.season,
        espn_s2=settings.espn_s2, swid=settings.swid,
    )
    league = client.league()
    team = league.team(settings.team_id)
    if team is None:
        client.close()
        raise RuntimeError(
            f"team {settings.team_id} not found. Run `python -m cli teams`."
        )

    week = last_complete_week()
    try:
        trends = roster_trends([p.player_id for p in team.roster], week)
    except Exception as exc:
        logger.warning("usage trends unavailable: %s", exc)
        trends = {}

    ctx = AgentContext(
        league=league,
        team=team,
        client=client,
        approvals=ApprovalService(ActionStore(), build_channel(settings)),
        week=week,
        dry_run=settings.dry_run,
        trends=trends,
        max_messages=max_messages,
    )
    return ctx, client


def run(
    instruction: str | None = None,
    *,
    model: str | None = None,
    max_messages: int = 3,
    max_iterations: int = 24,
    effort: str = "medium",
) -> RunResult:
    """One agent pass. Returns what it did, including when it did nothing."""
    settings = get_settings()
    ctx, client = build_context(max_messages=max_messages)
    set_context(ctx)

    task = instruction or (
        "Check my team for anything worth my attention right now. "
        "Remember: most runs should send nothing."
    )

    tool_calls: list[str] = []
    input_tokens = output_tokens = 0
    summary = ""

    try:
        anthropic_client = anthropic.Anthropic()
        runner = anthropic_client.beta.messages.tool_runner(
            model=model or settings.agent_model,
            max_tokens=4096,
            system=SYSTEM,
            tools=ALL_TOOLS,
            max_iterations=max_iterations,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": task}],
        )

        for message in runner:
            input_tokens += message.usage.input_tokens
            output_tokens += message.usage.output_tokens
            for block in message.content:
                if block.type == "tool_use":
                    tool_calls.append(block.name)
                elif block.type == "text" and block.text.strip():
                    summary = block.text.strip()
    finally:
        client.close()

    return RunResult(
        summary=summary,
        proposals=list(ctx.proposals),
        insights=list(ctx.insights),
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
