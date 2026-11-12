#!/usr/bin/env python3
"""Unit tests: run with `python3 tests/test_unit.py`."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, ROOT)
import budgetlib as B  # noqa: E402
import gate  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


# Durations and tags
for text, want in [("5m", 300), ("90s", 90), ("1h", 3600), ("1h30m", 5400), ("2.5m", 150),
                   ("open", "open"), ("OPEN", "open"), ("5", None), ("", None), ("abc", None), ("0m", None)]:
    check(f"duration {text!r}", B.parse_duration(text), want)
check("tag present", B.parse_tag("Run E2E [budget: 5m]"), (True, 300))
check("tag spacing/case", B.parse_tag("x [Budget:10m]"), (True, 600))
check("tag missing", B.parse_tag("Run E2E"), (False, None))
check("tag malformed", B.parse_tag("x [budget: 5]"), (True, None))

# Detach detection
cases = [
    ("npm run dev &", True),
    ("npm run dev & ", True),
    ("npm run dev &\n", True),
    ("nohup npm run dev > log 2>&1", True),
    ("FOO=1 nohup node server.js", True),
    ("sudo setsid foo", True),
    ("npm run dev & disown", True),
    ("/usr/bin/nohup x", True),
    ("npm test && echo ok", False),
    ("cmd > out 2>&1", False),
    ("cmd &> out", False),
    ("server & sleep 2; curl localhost; kill %1", False),
    ("echo 'a & b' | grep nohup", False),
    ("git commit -m \"fix: avoid nohup &\"", False),
    ("grep -n 'disown' file.txt", False),
    ("cat <<'EOF' > x.sh\nnohup foo &\nEOF", False),
    ("echo \"unbalanced", False),
    ("ls # trailing comment &", False),
]
for cmd, want in cases:
    check(f"detach {cmd!r}", gate.detach_problem(cmd) is not None, want)


# Gate end to end through stdin
def run_gate(payload):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "gate.py")], input=json.dumps(payload),
                       capture_output=True, text=True)
    if not p.stdout.strip():
        return "allow"
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


def bash(**ti):
    return {"tool_name": "Bash", "tool_input": ti}


check("gate bg without tag", run_gate(bash(command="npx playwright test", run_in_background=True,
                                           description="Run E2E")), "deny")
check("gate bg with tag", run_gate(bash(command="npx playwright test", run_in_background=True,
                                        description="Run E2E [budget: 5m]")), "allow")
check("gate bg open", run_gate(bash(command="npm run dev", run_in_background=True,
                                    description="Dev server [budget: open]")), "allow")
check("gate bg bad tag", run_gate(bash(command="x", run_in_background=True, description="[budget: 5]")), "deny")
check("gate bg budget-wait exempt", run_gate(bash(command=B.WAIT_BIN + " /tmp/x.output 5m",
                                                  run_in_background=True, description="wait")), "allow")
check("gate fg plain", run_gate(bash(command="npm test", description="Run tests")), "allow")
check("gate fg 5m timeout", run_gate(bash(command="npm test", timeout=300000)), "allow")
check("gate fg 10m timeout", run_gate(bash(command="npm test", timeout=600000)), "deny")
check("gate bg long timeout ok", run_gate(bash(command="npm test", timeout=600000, run_in_background=True,
                                               description="Tests [budget: 8m]")), "allow")
check("gate fg detach", run_gate(bash(command="npm run dev &")), "deny")
check("gate monitor ignored", run_gate({"tool_name": "Monitor", "tool_input": {"command": "tail -f x &"}}), "allow")
check("gate bad json", subprocess.run([sys.executable, os.path.join(ROOT, "gate.py")], input="not json",
                                      capture_output=True, text=True).returncode, 0)

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
