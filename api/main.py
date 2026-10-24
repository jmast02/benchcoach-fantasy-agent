"""
Benchcoach web UI.

Session 1 scope: show the real league. No agent, no model, no writes -- just
your actual roster, your league, and this week's matchup on a page, so the
data layer is visibly correct before anything reasons on top of it.

The one piece of judgment here is `lineup_problems`, which flags starters who
are ruled out or questionable. It is deliberately dumb rule-checking rather
than anything clever -- it exists to show the shape of what the agent will do,
and to prove the injury data is actually usable.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import get_settings
from espn.client import ESPNClient, ESPNError
from espn.models import League, Player, Slot, Team

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Benchcoach", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def load_league() -> League:
    settings = get_settings()
    client = ESPNClient(
        settings.league_id,
        settings.season,
        espn_s2=settings.espn_s2,
        swid=settings.swid,
    )
    try:
        return client.league()
    finally:
        client.close()


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    settings = get_settings()

    if not settings.configured:
        return templates.TemplateResponse(
            request, "setup.html", {"settings": settings}
        )

    try:
        league = load_league()
    except ESPNError as exc:
        return templates.TemplateResponse(
            request, "setup.html", {"settings": settings, "error": str(exc)}
        )

    team = league.team(settings.team_id) if settings.team_id else None
    problems = lineup_problems(team) if team else []
    alternatives = (
        {p["player"].player_id: bench_alternatives(team, p["player"]) for p in problems}
        if team
        else {}
    )

    upgrades = bench_upgrades(team) if team else []
    standings = sorted(league.teams, key=lambda t: (-t.wins, -t.points_for))
    matchup = league.matchup_for(team.team_id) if team else None
    opponent = None
    if matchup and team:
        other_id = (
            matchup.away_team_id
            if matchup.home_team_id == team.team_id
            else matchup.home_team_id
        )
        opponent = league.team(other_id) if other_id else None

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "league": league,
            "team": team,
            "problems": problems,
            "alternatives": alternatives,
            "upgrades": upgrades,
            "standings": standings,
            "opponent": opponent,
            "settings": settings,
            "Slot": Slot,
        },
    )


@app.get("/healthz")
def healthz():
    settings = get_settings()
    payload = {
        "ok": True,
        "league_configured": settings.configured,
        "has_credentials": settings.has_credentials,
        "dry_run": settings.dry_run,
    }
    if settings.configured:
        try:
            league = load_league()
            payload["league"] = league.name
            payload["teams"] = len(league.teams)
            payload["week"] = league.current_week
        except ESPNError as exc:
            payload["ok"] = False
            payload["error"] = str(exc)
    return JSONResponse(payload)
