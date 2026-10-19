#!/usr/bin/env python3
"""Find, list, and resume-match saved sessions.

Usage:
    find_session.py --title "finance cashflow"               # find by title (all files)
    find_session.py --title "..." --harness pi               # ...within one harness's file
    find_session.py --title "..." --pick latest              # ...only the newest match of that title
    find_session.py --list                                    # list this harness's saved entries
    find_session.py --list --harness <harness>               # list one harness's entries
    find_session.py --list --all                             # list every harness's entries

`--list` auto-detects the *current* harness from environment hints for Pi,
Claude Code, and OpenCode, and lists every saved entry in that harness's record
file with its exact resume command. Without an env hint it refuses to guess and
asks for --harness or --all. Passing --title too filters the listing. Reads only
— record files are never modified here.

Prints JSON: {"matches": [{"file", "harness", "title", "session_id",
"model", "dir", "saved", "resume"}]} and a human summary. No matches: exit 1.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HARNESS_BY_FILE = {
    "pi.md": "pi",
    "claude-code.md": "claude-code",
    "codex.md": "codex",
    "antigravity.md": "antigravity",
    "opencode.md": "opencode",
}

RESUME = {
    "pi": "pi --session {sid}",
    "claude-code": "claude --resume {sid}",
    "codex": "codex resume {sid}",
    "antigravity": "agy --conversation {sid}",
    "opencode": "opencode --session {sid}",
}


def sessions_dir():
    # expanduser: ``AGENTS_MEMORIES_DIR=~/.agents/...`` is natural to write and PowerShell
    # does not expand it, so without this the script silently looks in a literal ./~ dir.
    raw = os.environ.get("AGENTS_MEMORIES_DIR", str(Path.home() / ".agents/memories/sessions"))
    return Path(os.path.expanduser(raw))


def auto_harness():
    """The current harness from env hints only — never a guess."""
    if os.environ.get("PI_SESSION_ID"):
        return "pi"
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDECODE"):
        return "claude-code"
    if os.environ.get("OPENCODE"):
        return "opencode"
    return None


def harness_of(path, text):
    # Case-insensitive filename lookup: on case-insensitive filesystems a record file can be
    # named Pi.md, and a miss here would silently produce an entry with no resume command.
    known = HARNESS_BY_FILE.get(path.name.lower())
    if known:
        return known
    lines = text.splitlines()
    m = re.match(r"^#\s+Sessions\s+—\s+(.+)$", (lines[0] if lines else "").lstrip("\ufeff"), re.I)
    if m:
        return m.group(1).strip().lower().replace(" ", "-")
    return path.stem


def parse_entries(text):
    """(entries, problems) where entries are (title, fields) pairs.

    A heading carrying two `session_id:` lines means two sessions share one entry, and keeping
    the last value silently loses the first id — so it is reported instead. This happened here:
    a malformed record merged two sessions and a session id vanished without a word.
    """
    entries, cur, problems = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            cur = (line[3:].strip(), {})
            entries.append(cur)
        elif cur is not None and line.startswith("- "):
            body = line[2:].strip()
            if ":" in body:
                key, value = (part.strip() for part in body.split(":", 1))
                if key in cur[1]:
                    problems.append(
                        f"entry {cur[0]!r} has more than one {key}: line "
                        f"({cur[1][key]!r} and {value!r}; entry is unusable)"
                    )
                    cur[1]["_invalid"] = True
                cur[1][key] = value
    return entries, problems


def to_match(f, harness, title, fields):
    sid = fields.get("session_id")
    template = RESUME.get(harness)
    return {
        "file": f.name,
        "harness": harness,
        "title": title,
        "session_id": sid,
        "model": fields.get("model"),
        "dir": fields.get("dir"),
        "saved": fields.get("saved"),
        # None rather than an empty string when the harness or the id is unknown: an empty
        # resume command reads like a valid answer.
        "resume": template.format(sid=sid) if (template and sid and not fields.get("_invalid")) else None,
    }


def main():
    try:
        # Piped stdout on Windows uses the locale codec (cp1252); a non-ASCII title would
        # raise there, and agents always capture output through a pipe.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="Find and list saved sessions")
    ap.add_argument("--title", default=None, help="project title to look up (filters --list too)")
    ap.add_argument("--harness", choices=sorted(set(HARNESS_BY_FILE.values())), default=None)
    ap.add_argument("--sessions-dir", default=None)
    ap.add_argument("--pick", choices=["latest", "all"], default="all",
                    help="latest: only the most recently saved match of a --title lookup")
    ap.add_argument("--list", action="store_true", help="list saved entries instead of title search")
    ap.add_argument("--all", action="store_true", help="with --list: every harness's file")
    args = ap.parse_args()

    if not args.title and not args.list:
        ap.error("nothing to do: pass --title <name> or --list")
    if args.title is not None and not args.title.strip():
        ap.error("--title needs a non-empty value; a blank title would match every entry")
    if args.all and args.harness:
        ap.error("--all and --harness are mutually exclusive")
    if args.all and not args.list:
        ap.error("--all only makes sense with --list")
    if args.list and args.pick != "all":
        # Previously this was silently ignored, which reads as "the newest was used" when every
        # entry was in fact listed. A flag that does nothing must say so.
        ap.error("--pick applies to a --title lookup; --list shows every entry")

    base = Path(os.path.expanduser(args.sessions_dir)) if args.sessions_dir else sessions_dir()

    # --list: harness selection is current-harness by env hint, else explicit
    if args.list:
        if not args.harness and not args.all:
            detected = auto_harness()
            if detected is None:
                sys.exit("cannot tell which harness you are in — pass --harness <name> or --all")
            args.harness = detected

    matches, problems = [], []
    if base.is_dir():
        try:
            files = sorted(base.glob("*.md"))
        except (OSError, RuntimeError) as exc:
            files = []
            problems.append(f"record directory unreadable ({type(exc).__name__}: {exc})")
        for f in files:
            try:
                text = f.read_text(errors="ignore", encoding="utf-8-sig")
            except OSError as exc:
                problems.append(f"{f.name}: unreadable ({type(exc).__name__}: {exc})")
                continue
            harness = harness_of(f, text)
            entries, file_problems = parse_entries(text)
            problems += [f"{f.name}: {p}" for p in file_problems]
            if args.harness and harness != args.harness:
                continue
            query = (args.title or "").strip().lower()
            for title, fields in entries:
                if fields.get("_invalid"):
                    continue
                if query:
                    lowered = title.lower()
                    if query not in lowered and lowered not in query:
                        continue
                matches.append(to_match(f, harness, title, fields))

    matches.sort(key=lambda m: (m.get("saved") or "",))
    if matches and args.pick == "latest":
        matches = [matches[-1]]
    for m in matches:
        m["recommended"] = m is matches[-1]

    scope = ("list " + (args.harness or "all harnesses")) if args.list else f"find {args.title!r}"
    payload = {"sessions_dir": str(base), "scope": scope, "matches": matches}
    if problems:
        # Loud, not silent: a malformed record must not quietly lose a session id.
        payload["problems"] = problems
    print(json.dumps(payload, indent=2))
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    for m in matches:
        tag = "  <- most recent" if m["recommended"] and len(matches) > 1 else ""
        resume = m["resume"] or "(no session id recorded — cannot build a resume command)"
        print(f"- [{m['harness']}] {m['title']}  (saved {m['saved']})  ->  {resume}{tag}")
        if m["dir"]:
            # State the value without pretending one shell's quoting/change-drive rules are
            # portable to bash, PowerShell, and cmd.exe.
            print(f'    working directory: {m["dir"]} (resume from this directory)')
    if problems:
        sys.exit(1)
    if not matches:
        print(f"no saved sessions for {scope} in {base}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
