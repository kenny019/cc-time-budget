#!/usr/bin/env python3
"""PostToolUse[Bash] hook, run with async + asyncRewake.

When a Bash call leaves a background task (run_in_background, or a foreground call
that hit its timeout and was moved to the background), wait for it. Exit 0 quietly
if it finishes. If it passes its estimate or goes silent and idle, print a status
report to stderr and exit 2, which wakes the agent.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budgetlib as B  # noqa: E402

DEFAULT_ESTIMATE = 600   # background call with no tag (gate disabled or bypassed)


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    if d.get("tool_name") != "Bash":
        return 0
    ti = d.get("tool_input") or {}
    tr = d.get("tool_response") or {}
    task_id = tr.get("backgroundTaskId") if isinstance(tr, dict) else None
    B.debug(f"watch fired task={task_id} bg={ti.get('run_in_background')} resp={json.dumps(tr)[:300]}")
    if not task_id:
        return 0
    command = ti.get("command", "")
    if B.WAIT_BIN in command or "budget-wait" in command:
        return 0

    found, est = B.parse_tag(ti.get("description", ""))
    if est == "open":
        return 0
    path = B.find_output(d.get("session_id"), task_id)
    if not path:
        B.debug(f"watch: no output file for {task_id}")
        return 0

    now = time.time()
    if ti.get("run_in_background"):
        estimate = est if isinstance(est, int) else DEFAULT_ESTIMATE
        started = B.start_time(path)
    else:
        # Promoted after hitting its foreground timeout: the timeout already elapsed.
        fg_timeout = (tr.get("timedOutAfterMs") or ti.get("timeout") or 120000) / 1000.0
        started = now - fg_timeout
        estimate = est if isinstance(est, int) else max(120, int(fg_timeout))
        if estimate <= fg_timeout:
            estimate = int(fg_timeout * 2)
    deadline = started + estimate

    w = B.Watch(path, started, deadline, B.silence_threshold(estimate))
    reason = w.run()
    if reason in ("done", "gone"):
        B.record_history({"ts": int(time.time()), "cwd": d.get("cwd"), "command": command,
                          "estimate": estimate, "actual": int(time.time() - started),
                          "result": B.finished_marker(path) or reason})
        return 0
    B.save_pending(path, {"cwd": d.get("cwd"), "command": command, "estimate": estimate,
                          "started": started, "woke": reason})
    sys.stderr.write(B.report(w, reason, task_id, command, estimate, B.continue_hint(path)) + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never wake the agent because of a bug in this hook
        B.debug(f"watch crashed: {e!r}")
        sys.exit(0)
