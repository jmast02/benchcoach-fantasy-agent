# Benchcoach

An AI agent that watches your fantasy football team, works out what you'd
otherwise miss, and texts you — with the reasoning, not just the verdict.

It runs on a schedule, stays quiet when there's nothing worth saying, and
proposes changes you approve with one tap.

---

## What it actually produces

These are real outputs against a real 12-team ESPN league, unedited.

**It goes against the projection when the projection is misleading:**

> The whole week is a floor exercise, and there's exactly one leak: **Tyler
> Shough.** 15.3 projected, but his floor this season is **-0.2**. Brissett
> projects 14.2 with a 6.5 floor and basically the same ceiling. You're paying
> 1.1 points to delete the one outcome that loses you this game.
>
> It's only right *because* you're a heavy favourite. If you were chasing 20
> points, I'd leave Shough in.

**It reasons weeks ahead:**

> **Weeks 8–9 is where it falls apart.** Minnesota draws DET (32.8 ppg) then
> BUF (32.8) — a 29.9 average over three weeks.
>
> **The target is the Patriots.** NE's next three average 20.3 ppg, including
> Week 8 @ MIA, one of the two weakest offenses in the league at 14.0. NE is
> 60% owned, but no rival is short at D/ST, so don't burn a claim today — roll
> the Vikings through Week 7, then pick up New England before the Miami game.

**It finds leverage across the whole league:**

> **The exploit is QB.** Willie Beamen and Soakers R Us both have *no backup
> QB*, and they hold waiver priority #1 and #2. The pool behind them is bare:
> Penix at 14.6, Darnold at 12.9, then nothing. One takes Penix and the other
> is stuck. You're carrying Brissett as surplus — once Penix is gone, he's the
> best QB either can get. That's the week to ask for a real piece.

**And most of the time, it says nothing:**

> I checked and sent nothing. All nine starters are active with no injury tags,
> the zeros on your bench are byes, and no bench player out-projects a starter.
> Nothing here a friend would text you about.

---

## The design stance

**The model is never asked to do arithmetic.** Lineup optimisation, slot
eligibility, bye weeks, usage trends and playoff math are exact, so they stay
in deterministic code with tests. A model asked to add up projections is slower,
costlier and occasionally wrong at something a loop does perfectly.

**The model gets the genuinely ambiguous work**, which is most of the value:

- What a *questionable* tag means given practice participation — "did not
  practise all week" and "full participation" carry the same ESPN tag and
  opposite meanings
- Whether thin data supports any conclusion (a player with three games cannot
  be assessed, and saying so is the correct answer)
- Multi-step reasoning no single query answers — the streaming decision above
  needs schedule, opponent strength, waiver availability and rival needs
  combined
- **What is worth interrupting a human for**

**Restraint is a hard requirement, not a preference.** An assistant that texts
nine times a week gets muted, and a muted assistant is worthless however good
its analysis. The bar is *"would a knowledgeable friend text you this
unprompted?"* Most runs send nothing, and that is a successful run.

This split is also why a routine check costs about nine cents instead of a
dollar.

---

## What it knows that a projection doesn't

| signal | source | why it matters |
|---|---|---|
| Defense vs position | PPR allowed per game, from real results | Facing the #1 run defense is worth points the projection barely reflects |
| Game script | Vegas spread + total → implied team points | A heavy underdog abandons the run; a favourite kills clock |
| Floor / ceiling | Weekly score distribution | 14 projected with a −0.2 floor ≠ 14 projected with a 6.5 floor |
| Matchup stance | Your projected margin | Decides whether you want safety or variance **this week** |
| Usage trend | Snap share, target share, carries | Role changes show up here weeks before they show up in points |
| Playoff race | Full remaining schedule | A fading team sells cheap; a team racing you shouldn't be helped |

That last column is the point. Most tools sort by projection and stop.

---

## Schedule

Different information lands at different times, so each slot asks a *different*
question. Running the same check seven times would be noise.

| when | asks |
|---|---|
| Tue 9:00 | what just came off waivers |
| **Tue 9:30** | **weekly digest — the one guaranteed message** |
| Wed 17:00 | anything worth claiming before the deadline |
| Thu 17:00 | anyone compromised in a Thursday slot |
| Fri 17:00 | final practice reports — the real read on *questionable* |
| Sun 11:30 | inactives, ~90 min before kickoff — the critical one |
| Sun 16:00 | late window |

Friday is the slot most setups miss, and it's the only one that can tell a
questionable player who'll play from one who won't.

---

## Architecture

```
                          ┌──────────────────────────┐
   ESPN Fantasy API       │   nflverse (open data)   │
   (undocumented)         │   parquet on GitHub      │
          │               └────────────┬─────────────┘
          │                            │
   league, rosters,            snap counts, target share,
   matchups, waivers,          injury reports w/ practice,
   full schedule               Vegas spreads & totals
          │                            │
          └─────────────┬──────────────┘
                        ▼
        ╔═══════════════════════════════════════╗
        ║           analysis/                   ║   DETERMINISTIC
        ║  optimizer   lineup math, eligibility ║   exact answers,
        ║  trends      snap/target windows      ║   covered by tests,
        ║  matchup     def-vs-pos, game script  ║   no model involved
        ║  startsit    floor/ceiling, stance    ║
        ║  standings   playoff race, leverage   ║
        ║  league_intel blocking, scouting      ║
        ╚═══════════════════╤═══════════════════╝
                            │  exposed as 16 tools
                            ▼
        ╔═══════════════════════════════════════╗
        ║             agent/                    ║   JUDGEMENT
        ║  Claude Opus + tool loop              ║   ambiguity, triage,
        ║  · what does "questionable" mean      ║   explanation,
        ║  · is thin data sayable               ║   multi-step reasoning
        ║  · is this worth interrupting for     ║
        ║  proposes — never executes            ║
        ╚═══════════════════╤═══════════════════╝
                            ▼
        ╔═══════════════════════════════════════╗
        ║           approvals/                  ║   THE GATE
        ║  pending action → Telegram            ║   nothing happens
        ║  [✅ Do it]  [✕ Skip]                 ║   without a human yes
        ║  expires before kickoff · audit log   ║
        ╚═══════════════════╤═══════════════════╝
                            ▼
                      your phone
                            │
                   you tap, you apply it
             (the agent never writes to ESPN)
```

**→ [ARCHITECTURE.md](ARCHITECTURE.md)** explains every piece of this in
detail: how the agent loop actually works, why the deterministic/model split
is the central decision, the identity-matching problem between ESPN and
nflverse, and a log of seven bugs that ran clean while producing wrong
answers.

**Data sources, all free:** ESPN's fantasy API (undocumented, cookie auth) for
league state; [nflverse](https://github.com/nflverse) for snap counts, target
share, official injury reports with practice participation, and Vegas lines.

Player identity between the two is solved with the ffverse crosswalk's explicit
`espn_id`, not fuzzy name matching — **169 of 169** real players matched on a
live roster. Fuzzy matching is how you end up confusing two Josh Allens.

---

## Setup

```bash
make setup                  # venv + install
cp .env.example .env        # then fill it in
make teams                  # find your team id
make roster                 # confirm it's connected
make agent                  # run it once
```

**You need:** an ESPN league id, and `espn_s2` + `SWID` cookies if it's private
(DevTools → Application → Cookies → espn.com). An
[Anthropic API key](https://console.anthropic.com). A Telegram bot token from
[@BotFather](https://t.me/botfather) — one message, no registration, no cost.

Those cookies are **full ESPN account access**, not scoped tokens. They live in
`.env`, which is gitignored, and nowhere else.

```bash
make install-schedule       # launchd job, macOS
make listen                 # process replies
```

---

## Honest limits

**ESPN's fantasy API is undocumented and internal.** It can change without
notice, and auth is session cookies. Sleeper's official API is read-only and
Yahoo gated programmatic access in 2026, so there is no sanctioned alternative.
This is a personal tool for your own league, not a service others sign into.

**It never writes to ESPN.** Writes were designed and deliberately dropped:
applying a lineup change through an undocumented API carries real risk and
saves about five seconds of tapping. You approve, then you tap it yourself.

**A sleeping Mac runs nothing.** launchd fires a missed job once on wake, which
covers a closed lid but not a weekend away. Run it somewhere always-on if
Sunday alerts need to be reliable.

**It costs money.** Roughly $0.09–0.12 per alert slot, $0.31 for the digest —
about **$0.90 a week**. The model is the only paid component.

---

## Development

```bash
make test     # 72 tests
make lint
make help     # every target
```

Tests cover the parts where a silent error is expensive: slot and position
maps (getting one wrong starts a kicker at flex and raises no exception), the
approval gate (nothing is executable without an unexpired human yes), reply
parsing, trend windows, and league-need modelling.
