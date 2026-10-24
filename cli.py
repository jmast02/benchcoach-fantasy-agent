"""Benchcoach CLI. `python -m cli <command> --help`"""

from __future__ import annotations

import argparse
import sys

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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
