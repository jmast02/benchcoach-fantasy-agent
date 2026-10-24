"""
Generate a launchd job so the agent runs without a terminal open.

One launchd entry per scheduled slot rather than a single long-running
process. A persistent process is the obvious design and the wrong one on a
laptop: it dies when the machine sleeps and silently never comes back, so you
discover it stopped by not getting a Sunday alert. launchd owns the schedule,
starts a short-lived process per slot, and survives reboots.

The honest limit: a Mac that is asleep or shut at 11:30 on Sunday runs nothing.
launchd will fire a missed job once on wake, which covers a closed lid but not
a weekend away. For reliability, run it somewhere always-on -- `make docker-
schedule` or any small VPS.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

from scheduler.jobs import DEFAULT_TZ, JOBS

LABEL = "com.benchcoach.agent"

# cron day names -> launchd Weekday integers (Sunday = 0)
_WEEKDAY = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def build_plist(project_dir: Path, *, timezone: str = DEFAULT_TZ) -> dict:
    python = project_dir / ".venv" / "bin" / "python"
    logs = project_dir / "logs"

    intervals = []
    for job in JOBS:
        weekday = _WEEKDAY.get(job.day.lower())
        if weekday is None:
            continue
        intervals.append({"Weekday": weekday, "Hour": job.hour, "Minute": job.minute})

    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "cli", "due"],
        "WorkingDirectory": str(project_dir),
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(logs / "agent.log"),
        "StandardErrorPath": str(logs / "agent.err"),
        "EnvironmentVariables": {"TZ": timezone, "PYTHONUNBUFFERED": "1"},
        # Fire a missed run once on wake -- covers a closed lid, not a weekend
        # away.
        "RunAtLoad": False,
    }


def write(project_dir: Path | None = None, *, timezone: str = DEFAULT_TZ) -> Path:
    project = (project_dir or Path.cwd()).resolve()
    (project / "logs").mkdir(exist_ok=True)

    target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        plistlib.dump(build_plist(project, timezone=timezone), handle)
    return target


def main() -> int:
    target = write()
    print(f"wrote {target}")
    print()
    print("Load it:")
    print(f"  launchctl unload {target} 2>/dev/null; launchctl load {target}")
    print()
    print("Check it is registered:")
    print(f"  launchctl list | grep {LABEL}")
    print()
    print("Watch it run:")
    print("  tail -f logs/agent.log")
    print()
    print("Remove it:")
    print(f"  launchctl unload {target} && rm {target}")
    print()
    print("Note: a Mac that is asleep runs nothing. launchd fires a missed job")
    print("once on wake, which covers a closed lid but not a weekend away.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
