"""Shared helpers for the time-budget hooks.

The budget system has three parts:
  gate.py      PreToolUse[Bash]: background commands must carry an estimate
               ("[budget: 5m]" in the description); blocks escapes like `cmd &`.
  watch.py     PostToolUse[Bash], async + asyncRewake: waits until the estimate
               passes (or the job goes silent and idle), then wakes the agent
               with a status report. Exits silently if the job finishes first.
  budget-wait  CLI the agent runs in the background to keep waiting after a
               wake-up. Exits (and so notifies the agent) when the job ends,
               the new estimate passes, or the job looks hung.
"""
import collections
import glob
import json
import os
import re
import subprocess
import time

CODE = os.path.dirname(os.path.realpath(__file__))
HOME = os.environ.get("BUDGET_HOME") or os.path.expanduser("~/.claude/budget")   # state: logs, pending
HISTORY = os.path.join(HOME, "history.jsonl")
DEBUG_LOG = os.path.join(HOME, "debug.log")
WAIT_BIN = os.path.join(CODE, "budget-wait")

TAG_RE = re.compile(r"\[budget:\s*([^\]]*)\]", re.I)
DUR_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?$", re.I)
DONE_RE = re.compile(r"^\[(exited with code -?\d+|killed)\]\s*$")

TICK = int(os.environ.get("BUDGET_TICK", 10))                # seconds between checks
IDLE_CPU_SECONDS = 0.5    # CPU used across the whole process tree in the silence window
MIN_SILENCE = int(os.environ.get("BUDGET_MIN_SILENCE", 300))  # never call a job hung before 5 min of silence
GONE_TICKS = 3            # no process holds the output file and no exit marker
RECENT_GROWTH = 60        # output that grew this recently counts as "progressing"


def debug(msg):
    if os.environ.get("BUDGET_DEBUG"):
        os.makedirs(HOME, exist_ok=True)
        with open(DEBUG_LOG, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")


def parse_duration(text):
    """'5m' -> 300, '1h30m' -> 5400, '90s' -> 90, 'open' -> 'open'. Bare numbers are rejected."""
    t = (text or "").strip().lower()
    if t == "open":
        return "open"
    if not t or not re.search(r"[hms]$", t):
        return None
    m = DUR_RE.match(t)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    secs = int(h * 3600 + mi * 60 + s)
    return secs if secs > 0 else None


def parse_tag(description):
    """Returns (found, value): value is seconds, 'open', or None when the tag is malformed."""
    m = TAG_RE.search(description or "")
    if not m:
        return False, None
    return True, parse_duration(m.group(1))


def fmt(secs):
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def find_output(session_id, task_id, wait=15):
    """Locate <tmp>/claude-<uid>/<project>/<session>/tasks/<task>.output."""
    roots = {os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp", "/tmp", "/private/tmp"}
    uid = os.getuid()
    deadline = time.time() + wait
    while True:
        for root in roots:
            base = os.path.join(root, f"claude-{uid}")
            pats = [os.path.join(base, "*", session_id or "*", "tasks", f"{task_id}.output"),
                    os.path.join(base, "**", f"{task_id}.output")]
            for p in pats:
                hits = glob.glob(p, recursive=True)
                if hits:
                    return os.path.realpath(hits[0])
        if time.time() >= deadline:
            return None
        time.sleep(1)


def start_time(path):
    st = os.stat(path)
    return getattr(st, "st_birthtime", None) or st.st_ctime


def holders(path):
    try:
        out = subprocess.run(["lsof", "-t", path], capture_output=True, text=True, timeout=10).stdout
        return {int(p) for p in out.split()}
    except Exception:
        return set()


def _cputime_to_secs(t):
    # ps time= format: [[dd-]hh:]mm:ss.cc
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = [float(x) for x in t.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return days * 86400 + secs


def tree_stats(roots):
    """(process count, total CPU seconds, commands) for roots plus all descendants."""
    if not roots:
        return 0, 0.0, []
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,time=,state=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0, 0.0, []
    procs, kids = {}, collections.defaultdict(list)
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        procs[pid] = (parts[2], parts[3], parts[4])
        kids[ppid].append(pid)
    seen, stack = set(), [p for p in roots if p in procs]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(kids.get(p, []))
    cpu = sum(_cputime_to_secs(procs[p][0]) for p in seen)
    cmds = [f"{p} {procs[p][1]} {procs[p][2][:120]}" for p in sorted(seen)]
    return len(seen), cpu, cmds


def finished_marker(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        if tail and DONE_RE.match(tail[-1]):
            return tail[-1].strip("[]")
    except Exception:
        pass
    return None


def tail_lines(path, n=15, width=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []
    lines = [l.rstrip() for l in lines if l.strip()]
    return [l if len(l) <= width else l[:width] + " ..." for l in lines[-n:]]


def record_history(entry):
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(HISTORY, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _pending_path(output_path):
    import hashlib
    return os.path.join(HOME, "pending", hashlib.sha1(output_path.encode()).hexdigest() + ".json")


def save_pending(output_path, meta):
    """After a wake-up, remember the task so budget-wait can log its real duration."""
    try:
        p = _pending_path(output_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(meta, f)
    except Exception:
        pass


def pop_pending(output_path):
    p = _pending_path(output_path)
    try:
        with open(p) as f:
            meta = json.load(f)
        os.remove(p)
        return meta
    except Exception:
        return None


def peek_pending(output_path):
    try:
        with open(_pending_path(output_path)) as f:
            return json.load(f)
    except Exception:
        return None


def past_runs(command, limit=3):
    """Durations of earlier finished runs of the exact same command, newest first."""
    try:
        with open(HISTORY) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []
    same = [r for r in rows if r.get("command") == command and r.get("actual")]
    return [r["actual"] for r in same[-limit:]][::-1]


class Watch:
    """Watches a task's output file until it finishes, passes its deadline, or looks hung."""

    def __init__(self, path, started, deadline, silence):
        self.path, self.started, self.deadline, self.silence = path, started, deadline, silence
        self.size = -1
        self.last_growth = time.time()
        self.cpu_samples = collections.deque()  # (time, cpu seconds)
        self.gone = 0
        self.nproc, self.cmds = 0, []

    def poll(self):
        """Returns 'done', 'gone', 'deadline', 'idle' or None (keep waiting)."""
        now = time.time()
        if finished_marker(self.path):
            return "done"
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return "gone"
        if size != self.size:
            if self.size != -1:
                self.last_growth = now
            self.size = size
        pids = holders(self.path)
        self.nproc, cpu, self.cmds = tree_stats(pids)
        if not pids:
            self.gone += 1
            if self.gone >= GONE_TICKS:
                return "done" if finished_marker(self.path) else "gone"
        else:
            self.gone = 0
        self.cpu_samples.append((now, cpu))
        while self.cpu_samples and now - self.cpu_samples[0][0] > self.silence + TICK:
            self.cpu_samples.popleft()
        if now >= self.deadline:
            return "deadline"
        if self.silent_for(now) >= self.silence and self.window_cpu(now) is not None \
                and self.window_cpu(now) < IDLE_CPU_SECONDS:
            return "idle"
        return None

    def silent_for(self, now=None):
        return (now or time.time()) - self.last_growth

    def window_cpu(self, now=None):
        """CPU seconds used over (roughly) the silence window, or None if not enough samples yet."""
        now = now or time.time()
        if len(self.cpu_samples) < 2 or now - self.cpu_samples[0][0] < self.silence - TICK:
            return None
        return max(0.0, self.cpu_samples[-1][1] - self.cpu_samples[0][1])

    def recent_cpu(self):
        """CPU seconds in roughly the last minute."""
        if len(self.cpu_samples) < 2:
            return None
        now_t, now_c = self.cpu_samples[-1]
        old = [s for s in self.cpu_samples if now_t - s[0] >= 55] or [self.cpu_samples[0]]
        t0, c0 = old[-1]
        if now_t - t0 < 5:
            return None
        return max(0.0, now_c - c0), now_t - t0

    def run(self):
        while True:
            reason = self.poll()
            if reason:
                return reason
            time.sleep(TICK)


def diagnosis(w, reason):
    lines = tail_lines(w.path, 15)
    silent = w.silent_for()
    rc = w.recent_cpu()
    busy = rc is not None and rc[0] >= 1.0
    repetitive = len(lines) >= 8 and len(set(lines[-8:])) <= 2
    if reason == "idle":
        return "Silent and idle: the process tree is using almost no CPU and has printed nothing. " \
               "Likely hung (waiting on a server, port, lock, network, or input), not slow."
    if repetitive:
        return "Output is repeating the same lines. Likely a retry or polling loop that will not end on its own."
    if silent < RECENT_GROWTH:
        return "Output is still growing. Likely progressing, just slower than estimated."
    if busy:
        return f"Silent for {fmt(silent)} but busy on CPU. Either working quietly or spinning."
    return f"Silent for {fmt(silent)} with little CPU. Possibly stuck; check the last lines."


def report(w, reason, task_id, command, estimate, how_to_continue):
    now = time.time()
    elapsed = now - w.started
    rc = w.recent_cpu()
    cpu_txt = f"{rc[0]:.1f}s CPU in the last {fmt(rc[1])}" if rc else "CPU not measured yet"
    head = {
        "deadline": f"[budget] Background task {task_id} is past its estimate: running {fmt(elapsed)}, estimate {fmt(estimate)}.",
        "idle": f"[budget] Background task {task_id} looks hung: running {fmt(elapsed)} (estimate {fmt(estimate)}), "
                f"no output for {fmt(w.silent_for())} and almost no CPU.",
    }[reason]
    out = [head] + ([f"Command: {command[:300]}"] if command else []) + [
           f"Processes: {w.nproc} alive, {cpu_txt}",
           f"Output: {w.size} bytes, last grew {fmt(w.silent_for())} ago. File: {w.path}"]
    prev = past_runs(command) if command else []
    if prev:
        out.append("Earlier runs of this exact command took: " + ", ".join(fmt(p) for p in prev))
    out.append("Assessment: " + diagnosis(w, reason))
    lines = tail_lines(w.path, 15)
    out.append("Last output lines:" if lines else "Last output lines: (none)")
    out += ["  " + l for l in lines]
    if w.cmds:
        out.append("Process tree: " + " | ".join(w.cmds[-6:]))
    out += ["",
            "Decide now and tell the user what you chose and why:",
            f"- Still progressing: keep waiting with a new estimate. {how_to_continue}",
            f"- Hung or looping: stop it with TaskStop (task id {task_id}), find the cause from the output, "
            "fix it, and only then rerun (or rerun a narrower piece).",
            "Do not start a second copy while it runs, and do not poll with sleep."]
    return "\n".join(out)


def continue_hint(path):
    return (f"Run `{WAIT_BIN} '{path}' <duration, e.g. 5m>` with the Bash tool, run_in_background: true, "
            "description \"[budget: open] wait on task\". It notifies you when the task ends, the new "
            "estimate passes, or it looks hung.")


def silence_threshold(estimate):
    return max(MIN_SILENCE, int(estimate * 0.5))
