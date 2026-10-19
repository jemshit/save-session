#!/usr/bin/env python3
"""Check that every detector honours its output contract on this machine.

The detectors are the skill's evidence layer: an agent reads `ok`, `heuristic`, `source`, and
`reason` to decide whether to save silently or ask the user. A traceback, empty output, a
missing flag, or `ok: false` without a reason all break that contract — and CI used to run
these detectors with `|| true` and discard their output, so none of it could fail.

Stores may be absent on a given machine, which is a legitimate `ok: false` plus a reason. This
checks the shape of the answer, not its value.

    python3 tools/check_detector_output.py
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DETECT = REPO / "skills" / "save-session" / "scripts" / "detect_session.py"
HARNESSES = ("pi", "claude-code", "codex", "antigravity", "opencode")
REQUIRED = ("ok", "harness", "dir", "heuristic")


def run(harness):
    out = subprocess.run(
        [sys.executable, str(DETECT), "--harness", harness],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    )
    if not out.stdout.strip():
        return None, f"printed nothing (exit {out.returncode}) — {out.stderr.strip()[:200]}"
    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"stdout is not one JSON object ({exc})"


def problems(harness, payload):
    if payload is None:
        return ["no payload"]
    out = []
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        out.append(f"payload is missing {missing}")
        return out
    if payload["harness"] != harness:
        out.append(f"payload reports harness {payload['harness']!r}")
    if payload["ok"]:
        if not payload.get("session_id"):
            out.append("ok=true with no session id")
        if not payload.get("source"):
            # The source is the evidence an agent shows the user before writing a record.
            out.append("ok=true with no source")
    elif not payload.get("reason"):
        out.append("ok=false with no reason")
    return out


def describe(payload):
    if payload is None:
        return "no payload"
    return (f"ok={payload.get('ok')} heuristic={payload.get('heuristic')} "
            f"{payload.get('source') or payload.get('reason')}")


def main():
    failures = 0
    for harness in HARNESSES:
        payload, error = run(harness)
        found = [error] if error else problems(harness, payload)
        if found:
            failures += 1
            print(f"  FAIL {harness}: " + "; ".join(found))
        else:
            print(f"  ok   {harness}: {describe(payload)}")
    if failures:
        print(f"\nFAIL — {failures} detector(s) broke the output contract")
        return 1
    print("\nOK — all detectors returned the documented envelope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
