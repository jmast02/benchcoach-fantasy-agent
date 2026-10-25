# Benchcoach — Architecture & How It All Works

> **Who this is for:** A Python developer who has built web apps and data
> pipelines and wants to understand how an AI agent is actually wired together.
> Every concept is explained in ordinary terms first, then the AI-engineering
> reality. There is no magic in here, and that is the point.

---

## Table of Contents

1. [The Big Picture](#1-the-big-picture)
2. [Full Architecture Diagram](#2-full-architecture-diagram)
3. [How an Agent Actually Works](#3-how-an-agent-actually-works)
4. [The Central Design Decision](#4-the-central-design-decision)
5. [Component Deep Dives](#5-component-deep-dives)
   - [ESPN's Undocumented API](#51-espns-undocumented-api)
   - [nflverse — The Stats Brain](#52-nflverse--the-stats-brain)
   - [The Identity Problem](#53-the-identity-problem)
   - [Tools Are Just Functions](#54-tools-are-just-functions)
   - [The Approval Gate](#55-the-approval-gate)
   - [Telegram, and Why Not SMS](#56-telegram-and-why-not-sms)
   - [launchd — Running Unattended](#57-launchd--running-unattended)
6. [What a Projection Doesn't Know](#6-what-a-projection-doesnt-know)
7. [File-by-File Breakdown](#7-file-by-file-breakdown)
8. [Things That Broke, and Why](#8-things-that-broke-and-why)
9. [What Happens on a Sunday Morning](#9-what-happens-on-a-sunday-morning)
10. [What It Costs](#10-what-it-costs)

---

## 1. The Big Picture

In a normal web app, **a user asks and the program answers**. The control flow
is yours: you wrote every branch.

In an agent, **the program asks a model what to do next**, does it, and asks
again. You write the *capabilities*; the model writes the control flow at
runtime.

| Web app | This agent |
|---|---|
| User hits a URL | A schedule fires at 11:30 Sunday |
| View queries the DB | Model decides which of 16 tools to call |
| You wrote the if/else | The model picks the next step from the last result |
| Returns a page | Sends a text — or decides to stay quiet |

The last row matters most. **Deciding to send nothing is a real output.** An
assistant that texts nine times a week gets muted, and a muted assistant is
worthless however good its analysis.

---

## 2. Full Architecture Diagram

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

---

## 3. How an Agent Actually Works

This is the whole mechanism. It is smaller than people expect.

**Step 1 — we send three things to the model:**

```
system prompt  →  the instructions (restraint rules, how to judge injuries)
user message   →  "check my team"
tools          →  names, descriptions and arguments of 16 Python functions
```

**Step 2 — the model replies with either:**

- plain text (it's finished), or
- *"call `analyze_player` with `player_name='Swift'`"*

**Step 3 — our code runs that function.** It hits ESPN, or nflverse, or our own
arithmetic. The result goes back as another message.

**Step 4 — repeat** until the model stops asking and answers.

```python
# agent/loop.py, essentially
runner = client.beta.messages.tool_runner(
    model="claude-opus-5",
    system=SYSTEM,
    tools=ALL_TOOLS,
    messages=[{"role": "user", "content": task}],
)
for message in runner:        # the SDK drives the back-and-forth
    record(message)
```

### The part that surprises people

**The model never touches your data.** It cannot query ESPN or read the
database. It can only ask *our* functions, and our functions decide what comes
back. Every number it states arrived from a tool result.

**This is why it can't hallucinate your roster.** It is not recalling football
from training data; it is reading what `get_my_roster()` returned four
messages ago.

---

## 4. The Central Design Decision

**Deterministic where the answer is exact. Model where it is a judgement.**

If you remember one thing from this project, make it this.

| Question | Who answers | Why |
|---|---|---|
| Which legal lineup scores the most? | Python | Arithmetic. A loop is perfect; a model is slower, costlier and occasionally wrong |
| Can a kicker fill the flex slot? | Python | A rule. Encode it once, test it |
| Snap share over the last 3 games | Python | A windowed average |
| Who makes the playoffs? | Python | Counting |
| Does "questionable + did not practise" mean he plays? | **Model** | A probability judgement from unstructured signal |
| Is a three-game sample sayable? | **Model** | Knowing when to decline is judgement |
| Grab the Ravens D now, or wait? | **Model** | Schedule + opponent strength + availability + rivals, combined |
| Is any of this worth a text? | **Model** | Taste |

The lazy version of this project routes everything through the model and calls
it AI. It would cost roughly ten times as much, run slower, and be *worse* —
because models are mediocre at arithmetic and excellent at the ambiguous parts
that code cannot express.

---

## 5. Component Deep Dives

### 5.1 ESPN's Undocumented API

ESPN publishes no fantasy API. What exists is the internal one their own web
app calls, and that is what this uses.

```
reads:  lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{yr}/...
auth:   espn_s2 + SWID cookies
```

Two consequences worth stating plainly:

**It speaks in integers.** A player is not "a WR on the bench" — he is
`defaultPositionId: 3` in `lineupSlotId: 20`. Those maps live in
`espn/models.py` and nowhere else, because getting one wrong fails *silently*:
the code runs, the lineup looks plausible, and it starts a kicker at flex.

**Auth is a session cookie, not a scoped token.** `espn_s2` is full account
access. That is why this is a personal tool for your own league and not a
service strangers sign into, and why those values live only in a gitignored
`.env`.

Why not an official API? Sleeper's is official and free but **read-only**.
Yahoo gated programmatic access behind an application in 2026 and is also
read-only. There is no sanctioned alternative.

### 5.2 nflverse — The Stats Brain

[nflverse](https://github.com/nflverse) publishes NFL data as parquet files on
GitHub releases. Free, open, no key, no scraping.

| Loader | Gives us |
|---|---|
| `load_player_stats` | targets, target share, carries, PPR points, **opponent** |
| `load_snap_counts` | snap percentage per week |
| `load_injuries` | status **and practice participation** |
| `load_schedules` | full schedule, **Vegas spread and total** |
| `load_ff_playerids` | the crosswalk (below) |

Two of these do disproportionate work. **Practice participation** is how you
tell a "questionable" player who will play from one who will not — ESPN's
single tag cannot. And **Vegas lines** are the best free predictor of game
script: a ten-point underdog abandons the run, which guts a two-down back.

### 5.3 The Identity Problem

ESPN calls him player `4429205`. nflverse calls him `00-0036912`. Same human,
no shared key.

The obvious fix is matching on name, and it is a trap — there are two Josh
Allens, two Michael Thomases, and names differ in punctuation and suffixes.

`load_ff_playerids` carries an explicit `espn_id` alongside `gsis_id` and
`pfr_id`. Measured against a live 12-team league: **169 of 169 real players
matched exactly.** The thirteen misses were D/ST units, which are teams rather
than people and take a separate path.

When a crosswalk exists, use it. Fuzzy matching is a last resort that fails
quietly.

### 5.4 Tools Are Just Functions

A tool is a decorated Python function. Its **docstring is the API
documentation the model reads**:

```python
@beta_tool
def get_waiver_competition(position: str) -> str:
    """How many rivals also need this position, and their waiver priority.

    Tells you whether to claim now or wait.

    Args:
        position: One of QB, RB, WR, TE.
    """
```

The SDK turns that into a JSON schema automatically. The description is not
decoration — a vague docstring is the most common reason an agent calls the
wrong tool, or calls the right one with nonsense arguments.

Tool results are returned as **readable text, not JSON**. The model reads them,
and `snap share 65% → 50% over 3 games` conveys shape better than a nested
object.

Of the 16 tools, 14 are read-only. Exactly two have consequences
(`propose_lineup_swap`, `send_insight`), and neither changes a lineup.

### 5.5 The Approval Gate

The safety property, stated once:

> Nothing is executable unless a human approved it **and** the deadline has
> not passed.

Mechanically, every consequential action becomes a `PendingAction` row —
created, sent, answered, recorded. `is_actionable()` is the only thing that
grants permission, and it requires `APPROVED` and unexpired. It has tests,
because it is the property that matters most.

Expiry is not bookkeeping: applying a lineup change *after* kickoff is worse
than not applying it.

The store is SQLite and also the audit log. Every proposal, every answer, kept
permanently — "what did it do to my lineup and why" must be answerable later.

### 5.6 Telegram, and Why Not SMS

The original design was SMS. US carriers killed it.

A 10-digit long code cannot deliver application traffic without **A2P 10DLC
campaign registration** — a paid, multi-day process. Trial status does not
exempt you. The message left Twilio and died at the carrier (error 30034).

Telegram needs a bot token and nothing else, and is a better fit anyway:

- **Real buttons** — one thumb press instead of typing `YES 9PU` correctly
- **Long-polling** — no public webhook, no tunnel, no signature verification
- **Buttons are stripped once answered** — an old message cannot be tapped
  twice leaving you unsure which answer counted

`build_channel()` picks Telegram → SMS → console, so the loop still runs with
nothing configured at all. Free-text replies still work through a deliberately
forgiving parser (`y`, `ok`, `do it`, `nah`, `no thanks` all understood) that
**refuses to guess** — anything unclear asks again, because misreading "no" as
approval silently changes your lineup while misreading a real "yes" costs one
more message.

### 5.7 launchd — Running Unattended

Seven slots, one launchd entry each, rather than one long-running process.

A persistent scheduler is the obvious design and the wrong one on a laptop: it
dies when the machine sleeps and silently never returns, so the failure
surfaces as a *missing* Sunday alert. launchd owns the schedule, spawns a
short-lived process per slot, and survives reboots.

**Honest limit:** a Mac that is asleep runs nothing. launchd fires one
catch-up on wake — enough for a closed lid, not for a weekend away.

---

## 6. What a Projection Doesn't Know

ESPN's projection is roughly a season average nudged for form. Four things it
misses, all computed in `analysis/`:

**Matchup** — PPR points each defense allows to each position, from what
opposing players actually scored. Facing the #1 run defense is worth several
points the projection barely reflects.

**Game script** — the Vegas spread implies how a game gets played. A heavy
underdog throws; a heavy favourite runs clock. This is the best free predictor
of how volume gets shared.

**Shape** — floor, ceiling and volatility instead of the mean. Two players
projected 14 and 12 are not ranked if the first has a floor of −0.2 and the
second 6.5.

**Stance** — and which of those you *want* depends on your matchup. A heavy
favourite should fear variance; a team well behind loses slowly by playing
safe. Sorting by projection without checking this is the most common way to
give bad fantasy advice.

Put together, this is what let the agent recommend starting a **lower**-
projected QB, and state the condition under which it would not.

---

## 7. File-by-File Breakdown

```
espn/
  models.py        Slot/Position maps, Player/Team/League. The ID maps that
                   fail silently if wrong. Tested.
  client.py        Reads: league, rosters, matchups, free agents. Cookie auth.

stats/
  nflverse.py      Cached loaders + the espn_id crosswalk + last_complete_week
  trends.py        Snap/target/carry windows, rising-fading-thin-n/a

analysis/           ← all deterministic, all tested
  optimizer.py     Lineup problems, bench upgrades, eligibility
  matchup.py       Defense-vs-position, game script, floor/ceiling
  startsit.py      Player outlook, matchup stance
  standings.py     Playoff race, remaining SOS, trade leverage
  schedule.py      Lookahead, offensive strength, streaming windows
  league_intel.py  Roster needs, blocking, scouting, trade matching

agent/
  prompts.py       The system prompt. Restraint and honest uncertainty.
  tools.py         16 tools. 14 read-only, 2 consequential.
  loop.py          The tool-runner loop. ~60 lines.

approvals/
  models.py        PendingAction, LineupSwap, digit-leading codes
  store.py         SQLite store and permanent audit log
  service.py       Proposal, reply parsing, the actionable gate
  channels.py      Channel protocol: console, SMS
  telegram.py      Buttons, long-polling

scheduler/
  jobs.py          Seven slots, each with its own question
  install.py       launchd plist generator

api/main.py        FastAPI + Jinja: roster, alerts, trends
cli.py             teams · roster · watch · agent · job · listen · due
```

---

## 8. Things That Broke, and Why

The useful part of any project. Every one of these ran clean and produced
wrong answers.

**Sparse data read as a trend.** Week 7 had 92 snap rows against ~1,400 in
finished weeks — only the Thursday game had been played. Including a partial
week gave whoever played Thursday a "recent" window of one game while everyone
else got none. Since the agent runs mid-week *by design*, that was the normal
case. Fixed by anchoring on `last_complete_week()`.

**A definition that detected nothing.** "Need" was defined as *cannot field a
legal lineup* — rigorous-sounding and useless: across a real 12-team league in
week 7, no team was ever unable to fill a slot, so every report came back
empty. A manager starting two RBs with nobody behind them absolutely needs an
RB. Now means *no usable backup*.

**Codes that collided with English.** Approval codes were random letters, so
the generator could mint `NAH` and `YEP`. Replying "NAH" was answered with
*"I don't have anything with code NAH"* instead of being understood as a
rejection. Codes now always start with a digit, which makes them structurally
unable to look like words.

**A generator and matcher that drifted.** `2` was valid in a code's first
character but not the rest, so `7F2` could be *created* but never *matched*.
Now there is a property test over 500 generated codes.

**One ignored message retiring the agent.** The duplicate-proposal check looked
only at pending actions and never expired them. A proposal you ignored stayed
pending forever and permanently suppressed every future proposal for that
player. The agent would have gone quiet about a slot and never said why.

**A filter that was too broad.** Asking for available RBs returned every WR and
TE, because RB *eligibility* includes FLEX. Now filters on each position's
dedicated slot.

**A silent zero.** The free-agent response carries no `scoringPeriodId`, so the
week fell back to 1 and every projection read `0.0` — no error, just a pool of
players who all looked worthless.

The pattern: **none of these raised an exception.** They were found by running
against real data and looking at the output, which is the only way this class
of bug surfaces.

---

## 9. What Happens on a Sunday Morning

```
11:30  launchd fires → `python -m cli due`
       ↓
       resolves the slot: sunday-inactives
       ↓
       build_context(): fetch league, roster, matchups        [ESPN]
                        compute usage trends                  [nflverse]
       ↓
       tool loop begins ─────────────────────────────────────────┐
         get_my_roster          "what am I working with"         │
         get_matchup_strategy   "favoured by 36.8 → want floor"  │
         analyze_player ×N      investigates each close call     │
         get_player_detail      checks practice participation    │
         compare_start_sit      Shough vs Brissett               │
       ←───────────────────────────────────────────────────────┘
       ↓
       propose_lineup_swap(...)
       ↓
       PendingAction created, code 9RW, expires in 90 min
       ↓
       Telegram:  "Start Brissett over Shough (-1.0)
                   Shough's floor is -0.2 ...
                   [✅ Do it] [✕ Skip]"
       ↓
       you tap ✅ → `cli listen` records APPROVED
       ↓
       you apply it in the ESPN app (five seconds)
```

Most runs stop before the proposal and send nothing. That is the design
working.

---

## 10. What It Costs

| | |
|---|---|
| ESPN data | free |
| nflverse data | free |
| Telegram | free |
| Scheduling (launchd) | free |
| **Claude API** | **~$0.09–0.12 per alert, ~$0.31 for the digest** |
| | **≈ $0.90 / week** |

It is cheap *because* of section 4. The model only does the ambiguous work; the
arithmetic stays in Python. Routing everything through the model would cost
roughly ten times this and produce worse answers.
