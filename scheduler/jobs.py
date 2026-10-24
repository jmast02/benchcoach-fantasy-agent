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


JOBS: tuple[Job, ...] = (
    Job(
        name="waiver-morning",
        day="tue", hour=9, minute=0,
        instruction=(
            "Waivers have just processed. Look at who is now available at the "
            "positions where I am thin, and whether any of them is clearly "
            "better than someone I am rostering. Check whether rivals need the "
            "same position before telling me to hurry. Only message me if "
            "there is a specific player worth claiming."
        ),
    ),
    Job(
        name="weekly-digest",
        day="tue", hour=9, minute=30,
        digest=True,
        max_messages=1,
        effort="high",
        instruction=(
            "Write my weekly digest and send it with send_digest. Cover, "
            "briefly: how my matchup this week looks and against whom; anyone "
            "on my roster whose usage is clearly rising or falling, with the "
            "numbers; one or two waiver or streaming moves worth considering "
            "over the next two weeks, including the schedule reason; and "
            "anything about rivals' rosters that affects me. Keep it short "
            "enough to read on a phone. Skip any section that has nothing "
            "real in it rather than padding it."
        ),
    ),
    Job(
        name="waiver-deadline",
        day="wed", hour=17, minute=0,
        instruction=(
            "Waiver claims are due soon and last week's usage data has landed. "
            "Is there a claim I should put in tonight that I would regret "
            "missing? Only message me if the answer is yes and you can name "
            "the player."
        ),
    ),
    Job(
        name="thursday-lock",
        day="thu", hour=17, minute=0,
        instruction=(
            "Thursday night games lock soon. Do I have anyone in my starting "
            "lineup playing tonight who is injured, benched or otherwise "
            "compromised? This is the last chance to change them. Only message "
            "me if something is actually wrong."
        ),
        max_messages=1,
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
        effort="high",
        max_messages=3,
        instruction=(
            "Inactives are out and the early games kick off soon. Is anyone in "
            "my starting lineup ruled out, inactive or on a bye? If so, find "
            "the best eligible replacement on my bench and propose the swap. "
            "This is the most important run of the week -- act on anything "
            "real, but still do not message me about things that are fine."
        ),
    ),
    Job(
        name="sunday-late",
        day="sun", hour=16, minute=0,
        max_messages=2,
        instruction=(
            "Late-afternoon games kick off soon. Check only the starters whose "
            "games have not started yet for anyone ruled out, and propose a "
            "replacement if so."
        ),
    ),
)


def job_by_name(name: str) -> Job | None:
    return next((j for j in JOBS if j.name == name), None)


def run_job(job: Job):
    """Execute one job. Returns the agent's RunResult."""
    from agent.loop import run
    from agent.tools import ALL_TOOLS, DIGEST_TOOLS

    return run(
        instruction=job.instruction,
        max_messages=job.max_messages,
        effort=job.effort,
        tools=DIGEST_TOOLS if job.digest else ALL_TOOLS,
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
