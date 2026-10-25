"""
When the agent runs, and what it asks at each time.

The point is not to check more often -- it is that different information
arrives at different moments, so each slot asks a *different* question. Running
the same check six times would be noise; asking six different questions is a
week's worth of coverage.

Friday is the slot most people miss. It is when practice participation is
complete, and practice participation is the only way to tell a "questionable"
who will play from one who will not.

Every alert slot keeps the high interruption bar and usually sends nothing.
The Tuesday digest is the exception: it always sends, which gives the week one
reliable touchpoint and somewhere for "interesting but not urgent" to go
instead of interrupting.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TZ = "America/New_York"


@dataclass(frozen=True)
class Job:
    name: str
    day: str              # cron day-of-week
    hour: int
    minute: int
    instruction: str
    max_messages: int = 2
    effort: str = "medium"
    digest: bool = False

    @property
    def cron(self) -> str:
        return f"{self.minute} {self.hour} * * {self.day}"


# What is actually new on each day of the fantasy week. Injected into the
# morning brief so it looks at the right things rather than re-running the
# same survey seven times.
DAY_FOCUS = {
    "mon": (
        "Sunday's games are final. Look for injuries that happened yesterday, "
        "players whose role visibly changed, and who is likely to be dropped "
        "before waivers run."
    ),
    "tue": (
        "Waivers have processed overnight. Who is newly available, and did "
        "anything I wanted get taken."
    ),
    "wed": (
        "Last week's usage data has landed and waiver claims are due today. "
        "This is the main decision day of the week."
    ),
    "thu": (
        "Thursday night football is tonight and lineups lock for those "
        "players. Early injury designations are out."
    ),
    "fri": (
        "Practice reports are filling in. Questionable tags are starting to "
        "resolve."
    ),
    "sat": (
        "Quiet day. Only worth saying something if a real injury designation "
        "landed or a start-sit decision is still genuinely open for Sunday."
    ),
    "sun": (
        "Game day. Inactives come out about 90 minutes before kickoff, which "
        "a later run covers -- this is the early heads-up."
    ),
}

BRIEF_INSTRUCTION = (
    "Write my morning brief and send it with send_digest.\n\n"
    "First call get_previous_briefs. Your job is to tell me what has CHANGED "
    "since then, not to restate it. If little has changed, send two lines and "
    "stop -- padding a quiet day is how a daily brief stops being read.\n\n"
    "Today is {day}. {focus}\n\n"
    "Cover only what is real today: anything that changed on my roster, a "
    "move worth making, and how my matchup looks if it is close or if it is "
    "already decided. Skip any section with nothing in it."
)

JOBS: tuple[Job, ...] = (
    # Daily, every day. The memory of previous briefs is what keeps this from
    # becoming wallpaper.
    *(
        Job(
            name="morning-brief",
            day=day, hour=9, minute=0,
            digest=True,
            max_messages=1,
            effort="medium" if day in ("mon", "sat") else "high",
            instruction=BRIEF_INSTRUCTION.format(day=day.upper(), focus=focus),
        )
        for day, focus in DAY_FOCUS.items()
    ),
    Job(
        name="waiver-deadline",
        day="wed", hour=17, minute=0,
        instruction=(
            "Waiver claims are due soon. Is there a claim I should put in "
            "tonight that I would regret missing? Only message me if the "
            "answer is yes and you can name the player."
        ),
    ),
    Job(
        name="thursday-lock",
        day="thu", hour=17, minute=0,
        max_messages=1,
        instruction=(
            "Thursday night games lock soon. Do I have anyone in my starting "
            "lineup playing tonight who is injured, benched or otherwise "
            "compromised? This is the last chance to change them. Only "
            "message me if something is actually wrong."
        ),
    ),
    Job(
        name="friday-injuries",
        day="fri", hour=17, minute=0,
        effort="high",
        instruction=(
            "Final practice reports are out. For every starter with a "
            "questionable or doubtful tag, check practice participation and "
            "tell me what it actually means -- someone who did not practise "
            "all week is very different from someone who practised fully. "
            "Only message me about players where the answer changes what I "
            "should do."
        ),
    ),
    Job(
        name="sunday-inactives",
        day="sun", hour=11, minute=30,
        effort="high", max_messages=3,
        instruction=(
            "Inactives are out and the early games kick off soon. Is anyone "
            "in my starting lineup ruled out, inactive or on a bye? If so, "
            "find the best eligible replacement on my bench and propose the "
            "swap. This is the most important run of the week -- act on "
            "anything real, but still do not message me about things that "
            "are fine."
        ),
    ),
    Job(
        name="sunday-late",
        day="sun", hour=16, minute=0,
        max_messages=2,
        instruction=(
            "Late-afternoon games kick off soon. Check only the starters "
            "whose games have not started yet for anyone ruled out, and "
            "propose a replacement if so."
        ),
    ),
)


def job_by_name(name: str, *, day: str | None = None) -> Job | None:
    """
    Find a job by name, optionally disambiguating by day.

    The morning brief exists seven times under one name -- one per day, each
    with its own focus -- so looking it up without a day returns today's.
    """
    matches = [j for j in JOBS if j.name == name]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if day:
        exact = next((j for j in matches if j.day == day), None)
        if exact:
            return exact
    import datetime as dt
    today = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.date.today().weekday()]
    return next((j for j in matches if j.day == today), matches[0])


def run_job(job: Job):
    """Execute one job. Returns the agent's RunResult."""
    from agent.loop import run
    from agent.tools import ALL_TOOLS, DIGEST_TOOLS

    return run(
        instruction=job.instruction,
        max_messages=job.max_messages,
        effort=job.effort,
        tools=DIGEST_TOOLS if job.digest else ALL_TOOLS,
        # Brief jobs share one name across all seven days, so each morning's
        # run can read what the previous morning said.
        job_name=job.name if job.digest else None,
    )


def build_scheduler(timezone: str = DEFAULT_TZ):
    """A BlockingScheduler with every job registered."""
    import logging

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    log = logging.getLogger(__name__)
    scheduler = BlockingScheduler(timezone=timezone)

    for job in JOBS:
        def make(j=job):
            def _run():
                log.info("running job %s", j.name)
                try:
                    result = run_job(j)
                    log.info(
                        "job %s finished: %d message(s), ~$%.3f",
                        j.name, result.messages_sent, result.cost_estimate(),
                    )
                except Exception:
                    # One failing job must not kill the scheduler -- a crash on
                    # Wednesday should not mean silence on Sunday.
                    log.exception("job %s failed", j.name)
            return _run

        scheduler.add_job(
            make(),
            CronTrigger(day_of_week=job.day, hour=job.hour, minute=job.minute),
            id=job.name,
            name=job.name,
            misfire_grace_time=1800,
            coalesce=True,
        )
    return scheduler
