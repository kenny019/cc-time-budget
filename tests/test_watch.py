#!/usr/bin/env python3
"""Watcher tests against real processes: `python3 tests/test_watch.py` (~1 min)."""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, ROOT)
import budgetlib as B  # noqa: E402

B.TICK = 1
B.RECENT_GROWTH = 3
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


def task(script):
    """Start a shell whose stdout is the 'task output file', like the Bash tool does."""
    fd, path = tempfile.mkstemp(suffix=".output")
    os.close(fd)
    f = open(path, "w")
    wrapped = f"{{ {script}\n}}; echo; echo \"[exited with code $?]\""
    p = subprocess.Popen(["/bin/zsh", "-c", wrapped], stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL)
    f.close()
    return p, path


def watch(path, estimate, silence):
    now = time.time()
    w = B.Watch(path, now, now + estimate, silence)
    t0 = time.time()
    reason = w.run()
    return w, reason, time.time() - t0


# 1. Finishes before the estimate: done, no wake.
p, path = task("echo hi; sleep 3; echo bye")
w, reason, took = watch(path, 30, 20)
check("finishes early -> done", reason, "done")
check("finishes early -> quick", took < 10, True)

# 2. Busy and printing past the estimate: deadline, assessed as progressing.
p, path = task("for i in $(seq 1 40); do echo step $i; sleep 0.5; done")
w, reason, took = watch(path, 5, 20)
check("busy past estimate -> deadline", reason, "deadline")
check("deadline diagnosis says progressing", "progressing" in B.diagnosis(w, reason), True)
rep = B.report(w, reason, "t123", "for ...", 5, B.continue_hint(path))
check("report has task id and tail", "t123" in rep and "step" in rep, True)
p.kill()

# 3. Silent and idle (blocked forever on a read): idle wake before the estimate.
p, path = task("echo starting; cat /dev/stdin > /dev/null; sleep 600")
w, reason, took = watch(path, 120, 6)
check("silent+idle -> idle", reason, "idle")
check("idle wake before estimate", took < 30, True)
check("idle diagnosis says hung", "hung" in B.diagnosis(w, reason), True)
p.kill()

# 3b. Silent but burning CPU: not called idle; waits for the deadline.
p, path = task("echo spinning; yes > /dev/null")
w, reason, took = watch(path, 12, 5)
check("silent+busy -> deadline, not idle", reason, "deadline")
check("busy diagnosis mentions CPU", "CPU" in B.diagnosis(w, reason), True)
subprocess.run(["pkill", "-P", str(p.pid)])
p.kill()

# 4. Repetitive output: flagged as a loop.
p, path = task("while true; do echo 'waiting for server...'; sleep 0.3; done")
w, reason, took = watch(path, 4, 20)
check("repetitive -> loop diagnosis", "loop" in B.diagnosis(w, reason), True)
p.kill()

# 5. Process vanishes without an exit marker: gone.
fd, path = tempfile.mkstemp(suffix=".output")
os.close(fd)
w, reason, took = watch(path, 60, 30)
check("no holder, no marker -> gone", reason, "gone")

# 6. Killed task marker counts as done.
fd, path = tempfile.mkstemp(suffix=".output")
with os.fdopen(fd, "w") as f:
    f.write("partial\n\n[killed]\n")
check("killed marker", B.finished_marker(path), "killed")

# 7. Overrun task: watch saved it as pending at the wake-up; budget-wait logs the real duration.
state = tempfile.mkdtemp()
env = dict(os.environ, BUDGET_HOME=state, BUDGET_TICK="1")
p, path = task("for i in 1 2 3 4; do echo build $i; sleep 1; done")
started = time.time() - 30
B.HOME, B.HISTORY = state, os.path.join(state, "history.jsonl")
B.save_pending(path, {"cwd": "/x", "command": "make build", "estimate": 20, "started": started, "woke": "deadline"})
out = subprocess.run([os.path.join(ROOT, "budget-wait"), path, "1m"], env=env, capture_output=True, text=True).stdout
check("budget-wait reports finish", "finished after" in out, True)
rows = [l for l in open(B.HISTORY)] if os.path.exists(B.HISTORY) else []
check("overrun logged once", len(rows), 1)
check("overrun entry has command and real duration",
      '"make build"' in rows[0] and '"overran": true' in rows[0] if rows else False, True)
check("pending cleared", B.peek_pending(path), None)

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
