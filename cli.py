"""Benchcoach CLI. `python -m cli <command> --help`"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from analysis.optimizer import bench_upgrades, lineup_problems
from approvals.channels import build_channel
from approvals.models import LineupSwap
from approvals.service import ApprovalService
from approvals.store import ActionStore
from config import get_settings
from espn.client import ESPNClient, ESPNError


def _client(args) -> ESPNClient:
    settings = get_settings()
    league_id = args.league or settings.league_id
    if not league_id:
        print(
            "No league configured. Set BENCHCOACH_LEAGUE_ID in .env, or pass "
            "--league. It's the leagueId in your ESPN league URL.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return ESPNClient(
        league_id,
        args.season or settings.season,
        espn_s2=settings.espn_s2,
        swid=settings.swid,
    )


def cmd_teams(args) -> int:
    """List every team, so you can find your own team id."""
    client = _client(args)
    try:
        league = client.league()
    except ESPNError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        client.close()

    print(f"{league.name}  ({league.season}, week {league.current_week})")
    print(f"{'id':>3}  {'team':28} {'owner':20} {'rec':>7} {'PF':>8}")
    for team in sorted(league.teams, key=lambda t: -t.points_for):
        print(
            f"{team.team_id:>3}  {team.name[:28]:28} {(team.owner or '-')[:20]:20} "
            f"{team.record:>7} {team.points_for:>8.1f}"
        )
    print("\nSet BENCHCOACH_TEAM_ID in .env to your team's id.")
    return 0


def cmd_roster(args) -> int:
    settings = get_settings()
    client = _client(args)
    try:
        league = client.league()
    except ESPNError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        client.close()

    team_id = args.team or settings.team_id
    team = league.team(team_id) if team_id else None
    if team is None:
        print("Unknown team. Run `python -m cli teams` to list them.", file=sys.stderr)
        return 2

    print(f"{team.name}  {team.record}\n")
    for player in sorted(team.starters, key=lambda p: p.slot):
        flag = ""
        if player.is_out:
            flag = "  <-- OUT, still starting"
        elif player.is_risky:
            flag = f"  <-- {player.injury_status}"
        print(
            f"  {player.slot_name:5} {player.position_name:5} {player.name[:24]:26} "
            f"{player.projected_points or 0:>6.1f}{flag}"
        )
    print()
    for player in team.bench:
        status = f" [{player.injury_status}]" if player.is_out or player.is_risky else ""
        print(
            f"  {'BE':5} {player.position_name:5} {player.name[:24]:26} "
            f"{player.projected_points or 0:>6.1f}{status}"
        )
    return 0


def _service() -> ApprovalService:
    settings = get_settings()
    return ApprovalService(ActionStore(), build_channel(settings))


def cmd_watch(args) -> int:
    """
    Check the lineup and propose anything worth doing.

    This is what the scheduler will run before kickoff. For now the judgement
    is deterministic -- injured starters and projection gaps -- and the agent
    will replace that with something that understands "questionable".
    """
    settings = get_settings()
    client = _client(args)
    try:
        league = client.league()
    except ESPNError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        client.close()

    team = league.team(args.team or settings.team_id)
    if team is None:
        print("Unknown team. Run `python -m cli teams`.", file=sys.stderr)
        return 2

    service = _service()
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(
        minutes=settings.approval_ttl_minutes
    )

    proposals = []
    for upgrade in bench_upgrades(team):
        out, incoming = upgrade["out"], upgrade["in"]
        proposals.append(
            (
                LineupSwap(
                    out_player_id=out.player_id, out_name=out.name,
                    out_slot=int(out.slot), out_projected=out.projected_points or 0,
                    in_player_id=incoming.player_id, in_name=incoming.name,
                    in_slot=int(incoming.slot), in_projected=incoming.projected_points or 0,
                ),
                f"{incoming.name} projects {incoming.projected_points:.1f} vs "
                f"{out.name} at {out.projected_points:.1f} in your {out.slot_name} slot.",
            )
        )

    hurt = [p for p in lineup_problems(team) if p["severity"] == "out"]
    for problem in hurt:
        print(f"note: {problem['player'].name} is {problem['detail']} and still starting")

    if not proposals:
        print(f"{team.name}: nothing worth asking you about.")
        return 0

    print(f"{team.name}: {len(proposals)} proposal(s)\n")
    sent = 0
    for swap, reason in proposals:
        action = service.propose_swap(
            swap, reason=reason, expires_at=expires, send=not args.quiet
        )
        if action is None:
            print(f"  (already asked about {swap.out_name})")
            continue
        sent += 1
        print(f"  [{action.code}] {action.summary}")
    if sent and args.quiet:
        print("\n(--quiet: recorded but not sent)")
    return 0


def cmd_reply(args) -> int:
    """Feed a reply in by hand, as if it had arrived by SMS."""
    service = _service()
    print(service.handle_reply(args.text))
    return 0


def cmd_agent(args) -> int:
    """Run the agent. It decides what, if anything, is worth telling you."""
    from agent.loop import run

    result = run(
        instruction=args.ask,
        max_messages=args.max_messages,
        effort=args.effort,
    )

    print(f"tools used: {', '.join(result.tool_calls) or 'none'}")
    print(f"messages sent: {result.messages_sent}")
    print(
        f"tokens: in={result.input_tokens:,} out={result.output_tokens:,} "
        f"(~${result.cost_estimate():.3f})"
    )
    if result.proposals:
        print("\nproposals:")
        for a in result.proposals:
            print(f"  [{a.code}] {a.summary}")
    if result.insights:
        print("\ninsights:")
        for m in result.insights:
            print(f"  {m}")
    print("\n--- agent summary ---")
    print(result.summary or "(no closing summary)")
    return 0


def cmd_listen(args) -> int:
    """
    Watch for replies and apply them.

    Long-polls Telegram, so no public URL or tunnel is needed. Each answered
    message has its buttons stripped afterwards, so an old proposal cannot be
    tapped a second time and leave you unsure which answer counted.
    """
    import time

    service = _service()
    channel = service.channel
    if not hasattr(channel, "poll"):
        print(
            f"The {channel.name} channel can't receive replies. "
            "Configure Telegram, or use `python -m cli reply` by hand.",
            file=sys.stderr,
        )
        return 2

    print(f"listening on {channel.name} (ctrl-c to stop)")
    deadline = time.monotonic() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            for event in channel.poll(timeout=args.poll_timeout):
                text = event.get("text", "")
                source = "button" if event.get("from_button") else "typed"
                answer = service.handle_reply(text)
                print(f"  <- {text!r} ({source})")
                print(f"  -> {answer}")
                try:
                    channel.send(answer)
                except Exception as exc:
                    print(f"     (could not confirm back: {exc})", file=sys.stderr)
                if event.get("from_button") and event.get("message_id"):
                    channel.clear_buttons(event["message_id"])
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_pending(args) -> int:
    service = _service()
    service.store.expire_stale()
    actions = service.store.recent(args.limit)
    if not actions:
        print("nothing proposed yet")
        return 0
    print(f"{'code':6} {'status':9} {'when':17} summary")
    for a in actions:
        when = a.created_at.astimezone().strftime("%b %d %H:%M")
        print(f"{a.code:6} {str(a.status):9} {when:17} {a.summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchcoach")
    parser.add_argument("--league", type=int, help="override league id")
    parser.add_argument("--season", type=int, help="override season")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("teams", help="list teams and find your team id")
    p.set_defaults(func=cmd_teams)

    p = sub.add_parser("roster", help="show a team's lineup")
    p.add_argument("--team", type=int, help="team id (defaults to BENCHCOACH_TEAM_ID)")
    p.set_defaults(func=cmd_roster)

    p = sub.add_parser("watch", help="check the lineup and propose changes")
    p.add_argument("--team", type=int)
    p.add_argument("--quiet", action="store_true", help="record but do not send")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("reply", help="simulate an inbound SMS reply")
    p.add_argument("text")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("agent", help="run the agent")
    p.add_argument("--ask", help="a specific question instead of the routine check")
    p.add_argument("--max-messages", type=int, default=3)
    p.add_argument("--effort", default="medium",
                   choices=["low", "medium", "high", "xhigh", "max"])
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("listen", help="watch for replies and apply them")
    p.add_argument("--seconds", type=int, help="stop after N seconds (default: forever)")
    p.add_argument("--poll-timeout", type=int, default=25)
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("pending", help="show proposals and their outcomes")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_pending)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
