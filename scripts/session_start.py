#!/usr/bin/env python3
"""SessionStart hook: tell the agent how time budgets work, so it gets them right the first time."""
import json

RULES = """Time budgets for long-running commands (cc-time-budget plugin):
- Run anything that may take over ~2 min (E2E suites, builds, installs, reviews) with run_in_background and a realistic estimate at the end of the description, e.g. "Run E2E suite [budget: 5m]". Use [budget: open] only for servers and watchers. Background calls without a budget are blocked, and so are foreground timeouts over 5 min.
- You are woken with a status report if the task runs past its estimate or goes silent and idle. Decide right away and tell the user why: still progressing means keep waiting with budget-wait and a new estimate (the report gives the exact command); silent and idle means stop it and find the cause before rerunning; repeating output means it is looping, so stop it.
- Never poll with sleep loops, never detach with & or nohup, and never rerun the identical command blindly."""

print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": RULES}}))
