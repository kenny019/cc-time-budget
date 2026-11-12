#!/usr/bin/env python3
"""PreToolUse[Bash] hook.

1. A background Bash call (run_in_background: true) must carry a time estimate in
   its description: "[budget: 5m]", or "[budget: open]" for servers and watchers.
   The estimate lives in the description, not the command, so permission rules
   still match the command unchanged.
2. A foreground call may not set a timeout above 5 min: nothing can check on it
   until the timeout, so anything that long belongs in the background with a budget.
3. A foreground call must not detach work the harness cannot track: a trailing `&`,
   nohup, disown or setsid. Such processes never notify on exit and outlive the call.

Fails open: any error in this hook allows the call.
"""
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budgetlib as B  # noqa: E402

OPERATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;", "\n"}
DETACHERS = {"nohup", "disown", "setsid"}
FG_TIMEOUT_MAX_MS = int(os.environ.get("BUDGET_FG_TIMEOUT_MAX_MS", 300000))

TAG_HELP = ('Add your realistic time estimate to the description, e.g. "Run E2E suite [budget: 5m]" '
            '(units s/m/h, e.g. 90s, 5m, 1h30m). Use "[budget: open]" only for things meant to run '
            'indefinitely, like dev servers or log watchers. Keep the command unchanged. If the task runs '
            'past the estimate, or goes silent and idle, you will be woken with a status report.')


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


def first_lines_before_heredoc(cmd):
    """Heredoc bodies are data, not shell: only analyze up to the first heredoc line."""
    out = []
    for line in cmd.splitlines():
        out.append(line)
        if re.search(r"<<-?\s*['\"]?[A-Za-z_]", line):
            break
    return "\n".join(out)


def tokens(cmd):
    lex = shlex.shlex(first_lines_before_heredoc(cmd), posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = "#"
    return list(lex)


def detach_problem(cmd):
    """Return a description of how the command detaches work, or None."""
    try:
        toks = tokens(cmd)
    except ValueError:
        return None  # unbalanced quotes etc.: fail open
    if not toks:
        return None
    # Strip trailing separators so `cmd &;` and `cmd & ` both count as trailing.
    while toks and toks[-1] in {";", "\n"}:
        toks.pop()
    if toks and toks[-1] == "&":
        return "it ends with `&`"
    at_cmd_start = True
    for t in toks:
        if t in OPERATORS:
            at_cmd_start = True
            continue
        if at_cmd_start:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
                continue  # VAR=value prefix, command word still to come
            name = os.path.basename(t)
            if name in DETACHERS:
                return f"it uses `{name}`"
            at_cmd_start = name in {"sudo", "exec", "command", "env", "time", "nice"}
    return None


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    if d.get("tool_name") != "Bash":
        return 0  # Monitor commands also reach Bash-matcher hooks; leave them alone
    ti = d.get("tool_input") or {}
    cmd = ti.get("command", "")
    if not cmd:
        return 0

    if ti.get("run_in_background"):
        if B.WAIT_BIN in cmd or "budget-wait" in cmd:
            return 0
        found, est = B.parse_tag(ti.get("description", ""))
        if not found:
            return deny("Background commands need a time estimate. " + TAG_HELP)
        if est is None:
            return deny("The [budget: ...] tag needs a duration with a unit, like [budget: 90s], "
                        "[budget: 5m] or [budget: 1h], or [budget: open]. " + TAG_HELP)
        return 0

    timeout = ti.get("timeout") or 0
    if timeout > FG_TIMEOUT_MAX_MS:
        return deny(f"A foreground timeout of {timeout // 1000}s means nothing can check on this command "
                    f"for that long. Keep foreground timeouts at {FG_TIMEOUT_MAX_MS // 60000} min or less. If "
                    "it may take longer, run it with run_in_background: true instead. " + TAG_HELP)

    problem = detach_problem(cmd)
    if problem:
        return deny(f"This foreground command detaches a process ({problem}). Detached processes are "
                    "untracked: you get no exit notification and they outlive the call. Instead, run the "
                    "long-lived part as its own Bash call with run_in_background: true. " + TAG_HELP +
                    " For a server plus a client in one step, start the server in the background first, "
                    "then run the client in a separate call.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        B.debug(f"gate crashed: {e!r}")
        sys.exit(0)
