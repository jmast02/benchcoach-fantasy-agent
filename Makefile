.PHONY: help setup teams roster watch agent digest job listen schedule install-schedule uninstall-schedule test lint fmt

VENV := .venv/bin
PY   := $(VENV)/python

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup:  ## create the venv and install
	python3 -m venv .venv && $(VENV)/pip install -q -e ".[dev]"
	@echo "now copy .env.example to .env and fill it in"

teams:  ## list league teams (find your team id)
	$(PY) -m cli teams

roster:  ## show your lineup
	$(PY) -m cli roster

watch:  ## deterministic lineup check, no model
	$(PY) -m cli watch

agent:  ## run the agent once
	$(PY) -m cli agent

ask:  ## make ask Q="should I start X or Y"
	$(PY) -m cli agent --effort high --ask "$(Q)"

digest:  ## send the weekly digest now
	$(PY) -m cli job weekly-digest

job:  ## make job N=sunday-inactives   (or: make job N=list)
	$(PY) -m cli job $(or $(N),list)

listen:  ## process Telegram replies
	$(PY) -m cli listen

schedule:  ## run all jobs on their schedule, in the foreground
	$(PY) -m cli schedule

install-schedule:  ## install the launchd job (macOS)
	$(PY) -m scheduler.install

uninstall-schedule:  ## remove the launchd job
	launchctl unload ~/Library/LaunchAgents/com.benchcoach.agent.plist 2>/dev/null || true
	rm -f ~/Library/LaunchAgents/com.benchcoach.agent.plist
	@echo "removed"

test:
	$(VENV)/pytest -q

lint:
	$(VENV)/ruff check .

fmt:
	$(VENV)/ruff check --fix . && $(VENV)/ruff format .
