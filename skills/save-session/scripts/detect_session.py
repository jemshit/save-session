#!/usr/bin/env python3
"""Detect the current coding-agent session's metadata.

Usage:
    detect_session.py --harness <pi|claude-code|codex|antigravity|opencode>
    detect_session.py                     # auto-detect harness from the environment
    detect_session.py --check            # verify session record files

Prints one JSON object on stdout:
    {"ok": true,  "harness": ..., "session_id": ..., "model": ..., "dir": ..., "source": ...}
    {"ok": false, "harness": ..., "session_id": null, ..., "reason": "..."}   (also exit 1)

Never guesses a session id: when detection fails, session_id is null and ok is false.
Reads are chunked so huge transcripts stay bounded; the SQLite schema is only used
read-only and only after the public CLI contract.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import record_store as store

HOME = Path.home()
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
CHUNK = 64 * 1024
MAX_JSONL_LINE = 4 * 1024 * 1024


def sessions_dir():
    raw = os.environ.get("AGENTS_MEMORIES_DIR", str(HOME / ".agents/memories/sessions"))
    return Path(os.path.expanduser(raw))


def utc_timestamp():
    """Current UTC time for ledger writes; never rely on a model's remembered date."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def claude_config_dir():
    raw = os.environ.get("CLAUDE_CONFIG_DIR", str(HOME / ".claude"))
    return Path(os.path.expanduser(raw))


def codex_home():
    raw = os.environ.get("CODEX_HOME", str(HOME / ".codex"))
    return Path(os.path.expanduser(raw))


def safe_glob(root, pattern):
    """List matching paths, treating an unreadable store as no usable evidence."""
    try:
        return list(Path(root).glob(pattern))
    except (OSError, RuntimeError):
        return []


def glob_with_status(root, pattern):
    """Return (paths, error), preserving a store traversal failure for callers.

    pathlib.glob() deliberately suppresses scandir errors, so an unreadable child directory
    cannot silently disappear and make an older session look current — the tree is walked
    first to catch that. But only as deep as the pattern itself can ever reach: a
    non-recursive pattern like "*/*.jsonl" looks exactly one level deep, so a permission
    error in some unrelated, deeper directory (e.g. an old dated folder elsewhere in the
    store) must not fail detection for every project.
    """
    root = Path(root)
    if not root.exists():
        return [], None
    segments = pattern.split("/")
    max_depth = None if segments[0] == "**" else len(segments) - 1
    errors = []

    def onerror(exc):
        errors.append(f"{type(exc).__name__}: {exc}")

    for dirpath, dirnames, _ in os.walk(root, onerror=onerror):
        if max_depth is not None and len(Path(dirpath).relative_to(root).parts) >= max_depth:
            dirnames[:] = []  # the pattern can never look any deeper than this
    if errors:
        return [], "; ".join(errors)
    try:
        return list(root.glob(pattern)), None
    except (OSError, RuntimeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"


def read_text(path, errors="ignore"):
    """Read a file as UTF-8 explicitly, tolerating a BOM.

    Without encoding=, read_text() uses the platform's locale codec — cp1252 on Windows —
    which raises UnicodeDecodeError on the UTF-8 session files these harnesses write.
    utf-8-sig also strips a byte-order mark, which a Windows editor may add and which would
    otherwise break a first-line heading sniff.
    """
    try:
        return Path(path).read_text(encoding="utf-8-sig", errors=errors)
    except OSError:
        return ""


def cwd_needles(cwd):
    """Strings to search for when a JSON store records a working directory.

    Windows paths are stored JSON-escaped (C:\\\\Users\\\\...), so searching for the raw path
    never matches there. Both JSON forms are included, because writers differ on whether
    non-ASCII characters are emitted raw (JSON.stringify) or \\u-escaped (json.dumps with its
    default ensure_ascii). A separator-normalized variant covers stores that write forward
    slashes even on Windows.
    """
    candidates = [
        cwd,
        json.dumps(cwd, ensure_ascii=False)[1:-1],
        json.dumps(cwd)[1:-1],
        cwd.replace("\\", "/"),
    ]
    seen, unique = set(), []
    for value in candidates:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def jsonl_cwd_status(path, cwd, limit=4 * 1024 * 1024):
    """Return ``match``, ``no_match``, or ``unreadable`` for a JSONL session store."""
    read = 0
    try:
        with open(path, "rb") as fh:
            while read < limit:
                raw = fh.readline(MAX_JSONL_LINE + 1)
                if not raw:
                    break
                read += len(raw)
                if len(raw) > MAX_JSONL_LINE and not raw.endswith(b"\n"):
                    # Count the discarded remainder too, and stop as soon as the budget is
                    # exhausted: a single oversized line (e.g. a base64 attachment, or a
                    # corrupt record with no newline at all) must not be read to its end
                    # regardless of how far past `limit` that end is.
                    while raw and not raw.endswith(b"\n") and read < limit:
                        raw = fh.readline(MAX_JSONL_LINE + 1)
                        read += len(raw)
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                recorded = value.get("cwd")
                payload = value.get("payload")
                if recorded is None and isinstance(payload, dict):
                    recorded = payload.get("cwd")
                if isinstance(recorded, str) and same_path(recorded, cwd):
                    return "match"
    except OSError:
        return "unreadable"
    return "no_match"


def find_cwd_in_file(path, cwd, limit=4 * 1024 * 1024):
    """True when a JSONL store records exactly this working directory."""
    return jsonl_cwd_status(path, cwd, limit=limit) == "match"


def same_path(a, b):
    """Compare two paths the way the local filesystem does: separators, case, and symlinks.

    normcase is a no-op on POSIX and folds case plus slashes on Windows, which is what makes a
    cache key written by another tool still match the current directory. The final resolve()
    covers symlinked prefixes: a path recorded through a link and the physical path that
    os.getcwd() reports are the same directory.
    """
    if not a or not b:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(str(p)))  # noqa: E731
    if norm(a) == norm(b):
        return True
    if str(a).replace("\\", "/").rstrip("/") == str(b).replace("\\", "/").rstrip("/"):
        return True
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return False


def opencode_db_candidates():
    """Where opencode keeps its database, across platforms and release channels.

    An explicit OPENCODE_DB wins outright, never merged with a fallback. `opencode debug paths
    db` is the tool's own authoritative answer and wins outright too, for the same reason: a
    release channel (stable vs. a beta/nightly build) can relocate the data directory, and
    merging in a stale database from a different channel or an old install could surface an
    outdated session as if it still applied — exactly the guess this skill's never-guess
    contract exists to rule out. XDG_DATA_HOME or ~/.local/share is used only when neither the
    override nor the CLI can answer.
    """
    explicit = os.environ.get("OPENCODE_DB")
    if explicit:
        return [Path(os.path.expanduser(explicit))]
    cli_path = opencode_cli_db_path()
    if cli_path:
        return [cli_path]
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return [Path(xdg) / "opencode" / "opencode.db"]
    return [HOME / ".local" / "share" / "opencode" / "opencode.db"]


def run_cli(args, timeout=20):
    """Run an external CLI the way the platform expects.

    On Windows a CLI is often a .cmd/.bat shim. CreateProcess cannot start a batch file
    directly, so a shim is also attempted through cmd.exe, passed as separate arguments (not
    as one pre-quoted string, which cmd.exe then re-interprets). Whichever form actually runs
    wins; None means the CLI is absent or could not be started at all.
    """
    executable = shutil.which(args[0])
    if not executable:
        return None
    commands = [[executable, *args[1:]]]
    if executable.lower().endswith((".cmd", ".bat")):
        commands.append(["cmd.exe", "/c", executable, *args[1:]])
    last = None
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return result
        last = result
    return last


def opencode_cli_db_path():
    """The database path opencode itself reports via `opencode debug paths db`, or None.

    None covers every reason it can't be asked: the CLI is absent, this subcommand predates
    the installed version, or it errored — callers fall back to a hardcoded default rather
    than guessing from a failed or unexpected response.
    """
    out = run_cli(["opencode", "debug", "paths", "db"])
    if out is None or out.returncode != 0 or not out.stdout.strip():
        return None
    line = out.stdout.strip().splitlines()[0].strip()
    return Path(line) if line else None


def find_in_file(path, needle, limit=4 * 1024 * 1024):
    """True if `needle` appears within the first `limit` bytes of a file (chunked)."""
    needle_b = needle.encode()
    carry = b""
    read = 0
    try:
        with open(path, "rb") as fh:
            while read < limit:
                chunk = fh.read(CHUNK)
                if not chunk:
                    return False
                read += len(chunk)
                if needle_b in carry + chunk:
                    return True
                carry = (carry + chunk)[-len(needle_b):]
    except OSError:
        return False
    return False


def structured_model(value, harness):
    """Model from known transcript event shapes, excluding quoted/tool-result JSON."""
    if not isinstance(value, dict):
        return None
    if harness == "claude-code" and value.get("type") == "assistant":
        message = value.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            return message["model"] or None
    if harness == "codex" and value.get("type") == "turn_context":
        payload = value.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            return payload["model"] or None
    return None


def latest_structured_model(path, harness):
    """Last JSON `model` value in a JSONL file, without loading the transcript at once."""
    latest = None
    pending = b""
    dropping_oversized_line = False

    def inspect(raw):
        nonlocal latest
        try:
            value = json.loads(raw.decode("utf-8", errors="ignore"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        found = structured_model(value, harness)
        if found:
            latest = found

    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                if dropping_oversized_line:
                    marker = chunk.find(b"\n")
                    if marker == -1:
                        continue
                    chunk = chunk[marker + 1:]
                    dropping_oversized_line = False
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if len(line) <= MAX_JSONL_LINE:
                        inspect(line)
                if len(pending) > MAX_JSONL_LINE:
                    pending = b""
                    dropping_oversized_line = True
        if pending and not dropping_oversized_line:
            inspect(pending)
    except OSError:
        return None
    return latest



def flag_value(argv, name, reject_dash=False):
    """Value of ``--name value`` or ``--name=value`` in argv.

    Many agent callers pass flags in the ``--flag=value`` form; a check for only the
    space-separated form would silently treat ``--id=foo`` as absent and fall through to
    auto-detection instead of using the id the caller explicitly gave — a confident wrong
    answer rather than an error. ``present`` is True once the flag appears at all, even if its
    value is missing or rejected, so the caller can still error instead of falling through.
    """
    prefix = name + "="
    for i, token in enumerate(argv):
        if token == name:
            if i + 1 >= len(argv) or (reject_dash and argv[i + 1].startswith("-")):
                return True, None
            return True, argv[i + 1]
        if token.startswith(prefix):
            return True, token[len(prefix):] or None
    return False, None


def dir_matches(directory, cwd):
    """True if `directory` is `cwd` or one of its ancestor paths (component-wise)."""
    if not directory:
        return False
    try:
        if same_path(directory, cwd):
            return True
        d, c = Path(directory).resolve(), Path(cwd).resolve()
        norm = lambda p: os.path.normcase(os.path.normpath(str(p)))  # noqa: E731
        return norm(d) in {norm(parent) for parent in c.parents}
    except (OSError, ValueError):
        return False


# --- harness detectors -------------------------------------------------------
# Each returns a result dict; failures carry ok=false + reason and never an id.


def detect_pi():
    sid = os.environ.get("PI_SESSION_ID")
    if not sid:
        return {"ok": False, "heuristic": False, "reason": "PI_SESSION_ID is not set"}
    provider = os.environ.get("PI_PROVIDER")
    model_id = os.environ.get("PI_MODEL")
    model = f"{provider}/{model_id}" if provider and model_id else model_id
    return {
        "ok": True,
        "heuristic": False,
        "session_id": sid,
        "model": model,
        "source": "env PI_SESSION_ID/PI_PROVIDER/PI_MODEL",
    }


def detect_claude_code():
    root = claude_config_dir() / "projects"
    cwd = os.getcwd()
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env_sid:
        if not UUID_RE.fullmatch(env_sid):
            return {"ok": False, "heuristic": True,
                    "reason": "CLAUDE_CODE_SESSION_ID is not a UUID"}
        hits, glob_error = glob_with_status(root, f"*/{env_sid}.jsonl")
        statuses = {p: jsonl_cwd_status(p, cwd) for p in hits}
        cwd_hits = [p for p, status in statuses.items() if status == "match"]
        note = ("CLAUDE_CODE_SESSION_ID can remain stale after --continue or an argument-free "
                "resume; confirm it before saving")
        if len(cwd_hits) == 1:
            return {
                "ok": True, "heuristic": True, "validated": True,
                "session_id": env_sid,
                "model": latest_structured_model(cwd_hits[0], "claude-code"),
                "source": f"env CLAUDE_CODE_SESSION_ID validated by {cwd_hits[0].name}",
                "note": note,
            }
        if len(cwd_hits) > 1:
            return {"ok": False, "heuristic": True,
                    "reason": "CLAUDE_CODE_SESSION_ID matches multiple transcripts for this directory"}
        if glob_error or "unreadable" in statuses.values():
            return {"ok": False, "heuristic": True,
                    "reason": "CLAUDE_CODE_SESSION_ID could not be validated because a transcript store is unreadable",
                    "note": note}
        if hits:
            return {"ok": False, "heuristic": True,
                    "reason": "CLAUDE_CODE_SESSION_ID transcript does not record this working directory",
                    "note": note}
        return {
            "ok": True, "heuristic": True, "validated": False,
            "session_id": env_sid, "model": None,
            "source": "env CLAUDE_CODE_SESSION_ID (no local transcript to validate)",
            "note": note,
        }
    if not root.exists():
        return {"ok": False, "heuristic": True, "reason": f"no {root}"}
    # Session transcripts are <session-uuid>.jsonl; other *.jsonl files
    # (agent summaries, memory) live in the same directory and must be skipped.
    # Scan every project directory even when the expected slug contains a match. Otherwise an
    # unreadable newer transcript under a colliding/version-specific slug can be hidden by an
    # older readable one and silently selected as current.
    transcript_paths, traversal_error = glob_with_status(root, "*/*.jsonl")
    transcript_statuses = {p: jsonl_cwd_status(p, cwd) for p in transcript_paths
                           if UUID_RE.fullmatch(p.stem)}
    candidates = [p for p, status in transcript_statuses.items() if status == "match"]
    unreadable = traversal_error is not None or "unreadable" in transcript_statuses.values()
    if unreadable:
        return {"ok": False, "heuristic": True,
                "reason": "Claude session transcript store is unreadable"}
    if not candidates:
        return {"ok": False, "heuristic": True, "reason": "no session transcript for this working directory"}
    stamped = []
    for candidate in candidates:
        try:
            stamped.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    if not stamped:
        return {"ok": False, "heuristic": True,
                "reason": "session transcripts for this working directory are unreadable"}
    newest = max(stamped)[1]
    return {
        "ok": True,
        # newest-uuid-file heuristic: with concurrent sessions in one directory the
        # newest transcript may not be the current one — callers must surface this.
        "heuristic": True,
        "session_id": newest.stem,
        "model": latest_structured_model(newest, "claude-code"),
        "source": f"transcript {newest.name} (newest in project dir)",
    }


def detect_codex():
    root = codex_home() / "sessions"
    if not root.exists():
        return {"ok": False, "heuristic": True, "reason": f"no {root}"}
    stamped = []
    rollout_paths, glob_error = glob_with_status(root, "**/rollout-*.jsonl")
    if glob_error:
        return {"ok": False, "heuristic": True,
                "reason": "Codex rollout store is unreadable"}
    for path in rollout_paths:
        if not UUID_RE.search(path.stem):
            continue
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    files = [path for _, path in sorted(stamped, reverse=True)]
    if not files:
        return {"ok": False, "reason": "no rollout files"}
    cwd = os.getcwd()
    statuses = {p: jsonl_cwd_status(p, cwd) for p in files}
    if "unreadable" in statuses.values():
        return {"ok": False, "heuristic": True,
                "reason": "Codex rollout files are unreadable"}
    chosen = next((p for p, status in statuses.items() if status == "match"), None)
    if chosen is None:
        # No rollout records this directory, so the newest one belongs to another project.
        # Naming it would be a guess, and no source label can make that guess true.
        return {
            "ok": False,
            "heuristic": True,
            "reason": (f"{len(files)} rollout(s) found, none records this directory; "
                       f"pass --id, or read the id from /status"),
        }
    sid = UUID_RE.search(chosen.stem).group(0)
    return {
        "ok": True,
        "heuristic": True,
        "session_id": sid,
        "model": latest_structured_model(chosen, "codex"),
        "source": f"rollout {chosen.name} records this directory",
    }


def detect_antigravity():
    cache = HOME / ".gemini/antigravity-cli/cache/last_conversations.json"
    new_format_cache = HOME / ".gemini/antigravity-cli/cache/projects.json"
    settings = HOME / ".gemini/antigravity-cli/settings.json"
    model = None
    notes = []
    if settings.exists():
        try:
            settings_data = json.loads(read_text(settings))
            if isinstance(settings_data, dict):
                model = settings_data.get("model")
        except (json.JSONDecodeError, ValueError):
            pass
    if model is not None:
        # the settings file holds the *default* model, not this session's model
        notes.append("model is the settings default, not necessarily this session's")
    result = {"ok": True, "heuristic": True, "model": model}

    def fail(reason):
        result.update({"ok": False, "reason": reason})
        if notes:
            result["note"] = "; ".join(notes)
        return result

    if not cache.exists():
        if new_format_cache.exists():
            # Newer Antigravity CLI releases replaced this cache with a workspace-to-project
            # mapping (projects.json) and moved conversation data into a SQLite store whose
            # schema is not public — reading it would be a guess, which this skill never does.
            return fail("this install uses the newer projects.json cache plus a SQLite "
                        "conversation store, which this detector cannot read yet; "
                        "pass --id, or use the /resume picker")
        return fail("no last_conversations.json")
    try:
        mapping = json.loads(read_text(cache))
    except (json.JSONDecodeError, ValueError):
        return fail("unreadable last_conversations.json")
    if not isinstance(mapping, dict):
        return fail("last_conversations.json is not a path-to-session mapping")
    cwd = os.getcwd()
    # Keys are absolute paths as that tool wrote them: compare the way the filesystem does,
    # so separator and drive-letter case differences still match. An unrelated workspace's
    # malformed entry is dropped rather than failing the whole cache — only this directory's
    # own entry needs to be trustworthy.
    usable = {key: value for key, value in mapping.items()
              if isinstance(key, str) and isinstance(value, str) and value}
    malformed = len(mapping) - len(usable)
    if malformed:
        notes.append(f"{malformed} other cache entr{'y is' if malformed == 1 else 'ies are'} malformed")
    sid = next((value for key, value in usable.items() if same_path(key, cwd)), None)
    if sid:
        result.update({"session_id": sid, "source": "last_conversations.json[cwd]"})
        if notes:
            result["note"] = "; ".join(notes)
        return result
    # No entry for this directory. The cache maps directories to their own conversations, so
    # what it holds are other workspaces' sessions. Reporting the most recently modified one,
    # or an arbitrary first value labelled "only entry", handed over a session id that was not
    # this session — so state the gap instead.
    if usable:
        return fail(f"no conversation recorded for this directory "
                    f"({len(usable)} known for other directories); "
                    f"pass --id, or use the /resume picker")
    return fail("conversation cache empty" if not malformed
                else "last_conversations.json contains only malformed entries")


def render_model(raw):
    if not raw:
        return None
    try:
        m = json.loads(raw)
        pid, mid = m.get("providerID"), m.get("id")
        return f"{pid}/{mid}" if pid and mid else str(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)


def opencode_db_rows(limit=100, with_status=False):
    """Rows from opencode's session table, or [] when unavailable.

    The schema is private and may change between opencode versions, so this is used only for
the model, and as a fallback when the CLI is unavailable. The CLI's own output is the public
    contract and already carries each session's directory.
    """
    all_rows, errors, existing = [], [], 0
    for db in opencode_db_candidates():
        if not db.exists():
            continue
        existing += 1
        con = None
        try:
            # as_uri() percent-encodes and converts separators to URI form; the raw
            # f-string form is invalid on Windows (backslashes, drive-letter colon).
            con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            all_rows.extend(con.execute(
                "SELECT id, model, directory, time_updated FROM session_v2 "
                "ORDER BY time_updated DESC LIMIT ?", (limit,)
            ).fetchall())
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"{db}: {type(exc).__name__}: {exc}")
        finally:
            if con is not None:
                con.close()
    all_rows.sort(key=lambda row: row[3] if isinstance(row[3], (int, float)) else 0,
                  reverse=True)
    unique = []
    seen = set()
    for row in all_rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        unique.append(row)
    rows = unique[:limit]
    return (rows, errors, existing) if with_status else rows


def opencode_db_find(sid=None, cwd=None, with_status=False):
    """(session_id, model, directory) from the database, or (None, None, None).

    Returns nothing rather than the newest row when there is no match: picking an unrelated
    session and calling it a match is the failure this lookup exists to avoid.
    """
    rows, errors, existing = opencode_db_rows(with_status=True)
    for s, model, directory, _ in rows:
        if sid is not None and s == sid:
            found = (s, render_model(model), directory)
            return (*found, errors, existing) if with_status else found
        if sid is None and cwd and directory and dir_matches(directory, cwd):
            found = (s, render_model(model), directory)
            return (*found, errors, existing) if with_status else found
    found = (None, None, None)
    return (*found, errors, existing) if with_status else found


def detect_opencode():
    """Public contract first: the CLI, whose output already includes each session's directory.

    The private SQLite schema is consulted only for the model, and as a fallback when the CLI
    is unavailable. Every source label states what actually happened, because that label is
    the evidence a user is asked to confirm before a session is recorded.
    """
    cwd = os.getcwd()
    out = run_cli(["opencode", "session", "list", "--format", "json"])
    sessions = []
    cli_problem = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        try:
            decoded = json.loads(out.stdout)
            if not isinstance(decoded, list) or not all(isinstance(r, dict) for r in decoded):
                cli_problem = "opencode session list returned an unexpected JSON shape"
            else:
                sessions = [r for r in decoded if isinstance(r.get("id"), str) and r["id"]]
        except json.JSONDecodeError:
            cli_problem = "opencode session list returned invalid JSON"
    sessions.sort(key=lambda r: r.get("updated")
                  if isinstance(r.get("updated"), (int, float)) else 0, reverse=True)

    for entry in sessions:
        directory = entry.get("directory")
        if directory and dir_matches(directory, cwd):
            _, model, _ = opencode_db_find(sid=entry["id"])
            return {
                "ok": True,
                "heuristic": True,
                "session_id": entry["id"],
                "model": model,
                "source": "opencode session list (session recorded for this directory)",
            }

    if sessions:
        # opencode knows sessions, but none for this directory. Another project's session is
        # not this session, so report the gap instead of handing one over.
        return {
            "ok": False,
            "heuristic": True,
            "reason": (f"opencode reports {len(sessions)} session(s), none for this directory; "
                       f"pass --id, or choose one with 'opencode session list'"),
        }

    db_sid, db_model, db_directory, db_errors, db_existing = opencode_db_find(
        cwd=cwd, with_status=True
    )
    if db_sid:
        return {
            "ok": True,
            "heuristic": True,
            "session_id": db_sid,
            "model": db_model,
            "source": "session_v2 (cwd match; opencode CLI unavailable)",
        }
    if db_existing and db_errors:
        reason = "opencode database exists but could not be read"
    else:
        reason = cli_problem or "no opencode session for this directory"
    return {"ok": False, "heuristic": True, "reason": reason}


HARNESSES = {
    "pi": detect_pi,
    "claude-code": detect_claude_code,
    "codex": detect_codex,
    "antigravity": detect_antigravity,
    "opencode": detect_opencode,
}

RECORD_HEADINGS = {filename: heading for filename, heading in store.HARNESSES.values()}
RECORD_FIELDS = store.RECORD_FIELDS
REQUIRED_RECORD_FIELDS = store.REQUIRED_RECORD_FIELDS
SAVED_RE = store.SAVED_RE


def record_entries(text):
    """Return parsed ledger entries and shape problems without discarding duplicate fields."""
    return store.parse_record(text)[0]


def check_mode():
    """Verify the session record files: entry counts, duplicate ids, malformed entries."""
    base = sessions_dir()
    files = sorted(safe_glob(base, "*.md")) if base.is_dir() else []
    report = {"ok": bool(files), "sessions_dir": str(base), "files": []}
    contents = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8-sig")
            unreadable = None
        except (OSError, UnicodeError) as exc:
            # A locked or permission-denied record file must not be reported as an empty
            # one: "the records are gone" is exactly the failure this command exists to
            # surface, and on Windows a held-open file is routine.
            text, unreadable = "", f"{type(exc).__name__}: {exc}"
        contents[f] = text
        parsed = record_entries(text)
        titles = [item["title"] for item in parsed]
        ids = [item["fields"].get("session_id") for item in parsed
               if item["fields"].get("session_id")]
        entry = {
            "file": f.name,
            "entries": len(titles),
            "titles": titles,
            "session_ids": ids,
            "duplicate_session_ids": sorted({i for i in ids if ids.count(i) > 1}),
        }
        if unreadable:
            entry["unreadable"] = unreadable
        report["files"].append(entry)
    if not files:
        report["reason"] = "no record files"
    # A duplicate session id means two entries describe one session, so a lookup can return
    # the wrong title for a valid id. Reporting it without failing let a caller read
    # "ok: true" from a corrupted ledger.
    problems = []
    if any("unreadable" in entry for entry in report["files"]):
        problems.append("an unreadable record file — its contents could not be checked")
    for f, entry in zip(files, report["files"]):
        if "unreadable" in entry:
            continue
        expected_heading = RECORD_HEADINGS.get(f.name.lower())
        if expected_heading is None:
            problems.append(f"{f.name}: unknown harness record filename")
        else:
            _, file_problems = store.validate_record(contents[f], expected_heading)
            problems.extend(f"{f.name}: {problem}" for problem in file_problems)
    if problems:
        report["ok"] = False
        report["problems"] = problems
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def detect_by_id(harness, user_id):
    """Validate a user-provided session id against the harness's store.

    The user's id is authoritative by declaration — this never overrides it, it only
    enriches (model) and reports whether the id was found in the local store
    (a miss can simply mean a session from another machine).
    """
    r = {
        "ok": True,
        "heuristic": False,
        "user_provided": True,
        "session_id": user_id,
        "source": "user-provided",
    }
    if harness == "pi":
        env = os.environ.get("PI_SESSION_ID")
        r["validated"] = env == user_id
        provider = os.environ.get("PI_PROVIDER")
        model_id = os.environ.get("PI_MODEL")
        r["model"] = f"{provider}/{model_id}" if provider and model_id else model_id
        if env and env != user_id:
            r["note"] = f"differs from PI_SESSION_ID={env} (current session) — using the user-provided id"
        return r
    if harness == "claude-code":
        root = claude_config_dir() / "projects"
        hits = safe_glob(root, f"*/{user_id}.jsonl") if root.exists() else []
        r["validated"] = len(hits) == 1
        if len(hits) == 1:
            r["model"] = latest_structured_model(hits[0], "claude-code")
            r["source"] = f"user-provided (transcript {hits[0].parent.name}/{hits[0].name})"
        elif len(hits) > 1:
            r["validated"] = False
            r["note"] = "multiple transcripts have this id; local validation is ambiguous"
        else:
            r["note"] = f"no transcript for this id in {root}"
        return r
    if harness == "codex":
        root = codex_home() / "sessions"
        hits = safe_glob(root, f"**/*{user_id}*.jsonl") if root.exists() else []
        r["validated"] = bool(hits)
        if hits:
            r["model"] = latest_structured_model(hits[0], "codex")
            r["source"] = f"user-provided (rollout {hits[0].name})"
        else:
            r["note"] = "no rollout file for this id in ~/.codex/sessions"
        return r
    if harness == "antigravity":
        cache = HOME / ".gemini/antigravity-cli/cache/last_conversations.json"
        new_format_cache = HOME / ".gemini/antigravity-cli/cache/projects.json"
        settings = HOME / ".gemini/antigravity-cli/settings.json"
        r["validated"] = False
        if settings.exists():
            try:
                settings_data = json.loads(read_text(settings))
                if isinstance(settings_data, dict):
                    r["model"] = settings_data.get("model")
            except (json.JSONDecodeError, ValueError):
                pass
        if cache.exists():
            try:
                mapping = json.loads(read_text(cache))
                r["validated"] = isinstance(mapping, dict) and user_id in mapping.values()
            except (json.JSONDecodeError, ValueError):
                pass
        if not r["validated"]:
            if not cache.exists() and new_format_cache.exists():
                r["note"] = ("this install uses the newer projects.json cache, which this "
                             "script cannot read yet — id left unvalidated")
            else:
                r["note"] = "id not in last_conversations.json (may be another workspace or machine)"
        return r
    if harness == "opencode":
        db_sid, model, _ = opencode_db_find(sid=user_id)
        r["validated"] = db_sid == user_id
        r["model"] = model
        if not r["validated"]:
            r["note"] = "id not in the local session store (may be another machine)"
        return r
    r["ok"] = False
    r["reason"] = f"no validator for harness {harness!r}"
    return r


def main():
    try:
        # On Windows a piped stdout uses the locale codec (cp1252), which raises on a
        # non-ASCII project title. Agents capture output through a pipe, so be explicit.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    if "--check" in sys.argv:
        sys.exit(check_mode())
    user_id = None
    id_present, id_value = flag_value(sys.argv, "--id", reject_dash=True)
    if id_present:
        if id_value is None:
            sys.exit("--id requires a value")
        user_id = id_value
    harness = None
    harness_present, harness_value = flag_value(sys.argv, "--harness")
    if harness_present:
        if harness_value is None:
            sys.exit("--harness requires a value")
        harness = harness_value
    if harness is None:
        if os.environ.get("PI_SESSION_ID"):
            harness = "pi"
        elif os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDECODE"):
            harness = "claude-code"
        elif os.environ.get("OPENCODE"):
            harness = "opencode"
        else:
            # Never guess: a wrong guess reads another harness's id space. The caller can
            # always pass --harness.
            print(json.dumps({
                "ok": False,
                "heuristic": False,
                "harness": None,
                "session_id": None,
                "model": None,
                "dir": os.getcwd(),
                "source": None,
                "reason": "cannot tell which harness this is; pass --harness",
                "saved": None,
                "records_dir": str(sessions_dir()),
            }))
            sys.exit(1)
    if harness not in HARNESSES:
        sys.exit(f"unknown harness {harness!r}; choose from {', '.join(HARNESSES)}")
    if user_id is not None and (not user_id.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in user_id)):
        result = {
            "ok": False, "heuristic": False, "harness": harness,
            "session_id": None, "model": None, "source": None,
            "reason": "--id must be non-empty and contain no control characters",
            "dir": os.getcwd(),
            "saved": None,
            "records_dir": str(sessions_dir()),
        }
        print(json.dumps(result))
        sys.exit(1)
    if user_id is not None:
        result = detect_by_id(harness, user_id)
    else:
        result = HARNESSES[harness]()
    result["harness"] = harness
    result.setdefault("session_id", None)
    result.setdefault("model", None)
    result.setdefault("source", None)
    result.setdefault("reason", None)
    result.setdefault("heuristic", True)
    result["dir"] = os.getcwd()
    result["saved"] = utc_timestamp() if result.get("ok") else None
    result["records_dir"] = str(sessions_dir())
    if result.get("ok") is None:
        result["ok"] = bool(result.get("session_id"))
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
