# cc-time-budget

A Claude Code plugin that gives long-running commands a time budget. Claude states
how long a background command should take; if it runs past that estimate, or goes
silent and idle, Claude is woken with a status report and decides whether to keep
waiting, stop it, or fix the cause.

## Why

In Claude Code, a background Bash command has no time limit in the desktop app or
terminal, and Claude is only told when it exits. A hung E2E suite, a build waiting
on a lock, or a polling loop that never ends can stall a session for hours until a
human notices ([anthropics/claude-code#88702](https://github.com/anthropics/claude-code/issues/88702),
[#61568](https://github.com/anthropics/claude-code/issues/61568)).

## Install

In Claude Code:

```
/plugin marketplace add kenny019/cc-time-budget
/plugin install time-budget@cc-time-budget
```

Or from a terminal:

```bash
claude plugin marketplace add kenny019/cc-time-budget
```

```bash
claude plugin install time-budget@cc-time-budget
```

Requires macOS or Linux with `python3`, `lsof` and `ps` on the PATH.

## What it does

**Estimates.** A background Bash call must end its description with a budget, for
example `Run E2E suite [budget: 5m]` (units `s`, `m`, `h`), or `[budget: open]` for
servers and watchers. Calls without one are blocked with a message that shows the
fix, so Claude corrects itself on the next try. The estimate lives in the
description, not the command, so your permission rules still match the command.

**Wake-ups.** When the estimate passes, or the task has printed nothing and used
almost no CPU for max(5 min, half the estimate), Claude is woken with:

- elapsed time against the estimate
- CPU use across the whole process tree and how long since output last grew
- the last output lines and the durations of earlier runs of the same command
- an assessment: still progressing, silent but busy, hung, or looping

Claude then keeps waiting with `budget-wait` and a new estimate, or stops the task
and investigates. Tasks that finish in time cause no wake-up at all.

**Guards.**
- Foreground timeouts over 5 minutes are blocked, since nothing can check on such a
  call until it times out.
- Foreground commands that detach work (a trailing `&`, `nohup`, `disown`, `setsid`)
  are blocked: those processes are untracked and never notify on exit. Starting a
  server and curling it within one command is still allowed.
- Foreground commands that Claude Code moves to the background at their timeout are
  watched too, with an estimate of max(2 min, 2x the timeout).

A SessionStart hook explains all this to Claude at the start of each session.

## How it works

| Script | Hook | Role |
|---|---|---|
| `scripts/gate.py` | PreToolUse (Bash) | Requires budgets, blocks long foreground timeouts and detaching. |
| `scripts/watch.py` | PostToolUse (Bash), `asyncRewake` | Waits on each new background task. Exits quietly if it finishes; otherwise exits 2 with the report, which wakes Claude. |
| `scripts/budget-wait` | run by Claude | Keeps waiting after a wake-up; exits when the task ends, the new estimate passes, or it looks hung. |
| `scripts/session_start.py` | SessionStart | Adds the usage rules to Claude's context. |

Both hooks fail open: a crash allows the call and never wakes Claude. Run history
(command, estimate, actual duration) is kept in `~/.claude/budget/history.jsonl`.

Notes:
- The desktop app labels a wake-up "Stop hook blocking error from command
  PostToolUse:Bash". That label is cosmetic; the content is the report.
- A wake-up for a subagent's task goes to that subagent.
- In `claude -p` and SDK runs, Claude Code still stops background commands at its own
  limit (10 to 30 minutes).
- Detach detection is best effort: `tmux -d`, `screen -dm` and `( cmd & )` are not caught.

## Development

```bash
python3 tests/test_unit.py
```

```bash
python3 tests/test_watch.py
```

Try the plugin without installing it:

```bash
claude --plugin-dir /path/to/cc-time-budget
```

Environment knobs for testing: `BUDGET_TICK` (10s), `BUDGET_MIN_SILENCE` (300s),
`BUDGET_FG_TIMEOUT_MAX_MS` (300000), `BUDGET_HOME` (state folder), `BUDGET_DEBUG=1`
(writes `debug.log`).

## License

MIT
