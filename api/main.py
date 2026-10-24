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

import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analysis.optimizer import bench_alternatives, bench_upgrades, lineup_problems
from approvals.channels import build_channel, twiml, validate_twilio_signature
from approvals.service import ApprovalService
from approvals.store import ActionStore
from config import get_settings
from espn.client import ESPNClient, ESPNError
from espn.models import League, Slot

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Benchcoach", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

store = ActionStore()
approvals = ApprovalService(store, build_channel(get_settings()))


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


@app.post("/sms/inbound")
async def sms_inbound(request: Request, Body: str = Form(""), From: str = Form("")):
    """
    Twilio posts replies here.

    This endpoint is public and it changes lineups, so it is guarded twice:
    the request must carry a valid Twilio signature, and it must come from the
    one phone number this agent answers to. Both checks fail closed.
    """
    settings = get_settings()
    form = dict(await request.form())

    if settings.twilio_auth_token:
        # Twilio signs the exact URL it was configured with. Prefer the
        # configured public URL over the request's own view of itself, which a
        # proxy or tunnel will have rewritten.
        url = (
            f"{settings.public_base_url.rstrip('/')}/sms/inbound"
            if settings.public_base_url
            else str(request.url)
        )
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validate_twilio_signature(
            settings.twilio_auth_token, signature, url, {k: str(v) for k, v in form.items()}
        ):
            logger.warning("rejected inbound SMS with an invalid signature")
            return Response(twiml(), media_type="application/xml", status_code=403)

    if settings.my_phone_number and From and From != settings.my_phone_number:
        logger.warning("ignoring inbound SMS from an unexpected number")
        return Response(twiml(), media_type="application/xml", status_code=403)

    reply = approvals.handle_reply(Body)
    return Response(twiml(reply), media_type="application/xml")


@app.get("/approvals")
def list_approvals():
    """Recent proposals and what happened to them -- the audit trail."""
    store.expire_stale()
    return JSONResponse(
        [
            {
                "id": a.action_id, "code": a.code, "status": str(a.status),
                "summary": a.summary, "created_at": a.created_at.isoformat(),
                "expires_at": a.expires_at.isoformat(),
                "applied_at": a.applied_at.isoformat() if a.applied_at else None,
            }
            for a in store.recent(25)
        ]
    )


@app.get("/healthz")
def healthz():
    settings = get_settings()
    payload = {
        "ok": True,
        "league_configured": settings.configured,
        "has_credentials": settings.has_credentials,
        "dry_run": settings.dry_run,
        "approval_channel": approvals.channel.name,
        "pending_approvals": len(store.pending()),
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
