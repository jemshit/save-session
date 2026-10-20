#!/usr/bin/env python3
"""Shared parsing, validation, title handling, and atomic writes for session ledgers."""

import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


HARNESSES = {
    "pi": ("pi.md", "# Sessions — Pi"),
    "claude-code": ("claude-code.md", "# Sessions — Claude Code"),
    "codex": ("codex.md", "# Sessions — Codex"),
    "antigravity": ("antigravity.md", "# Sessions — Antigravity"),
    "opencode": ("opencode.md", "# Sessions — OpenCode"),
}
HARNESS_BY_FILE = {filename: harness for harness, (filename, _) in HARNESSES.items()}
RECORD_FIELDS = {"saved", "session_id", "model", "dir"}
REQUIRED_RECORD_FIELDS = {"saved", "session_id", "dir"}
SAVED_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class RecordLocked(Exception):
    def __init__(self, path):
        self.path = Path(path)
        super().__init__(str(self.path))


def record_path(base, harness):
    filename, _ = HARNESSES[harness]
    return Path(base) / filename


def resolved_target(path):
    """Resolve file and directory symlinks so os.replace() updates the target, not a link."""
    return Path(path).resolve(strict=False)


def parse_record(text, expected_heading=None):
    """Return (entries, problems). Entries preserve order and duplicate-field evidence."""
    lines = text.lstrip("\ufeff").splitlines()
    entries, current, problems = [], None, []
    first = lines[0] if lines else ""
    if expected_heading is not None and first != expected_heading:
        problems.append(f"expected top heading {expected_heading!r}")

    for number, line in enumerate(lines[1:], 2):
        if not line:
            continue
        if line.startswith("## "):
            current = {"title": line[3:].strip(), "fields": {}, "duplicates": []}
            entries.append(current)
            continue
        if current is not None and line.startswith("- ") and ":" in line:
            key, value = (part.strip() for part in line[2:].split(":", 1))
            if key in current["fields"]:
                current["duplicates"].append(key)
                problems.append(
                    f"entry {current['title']!r} has duplicate field {key} — more than one {key}: line "
                    f"({current['fields'][key]!r} and {value!r}; entry is unusable)"
                )
            current["fields"][key] = value
            continue
        problems.append(f"line {number}: unexpected content")

    return entries, problems


def validate_record(text, expected_heading=None):
    """Parse a complete ledger and report every structural and field-level problem."""
    entries, problems = parse_record(text, expected_heading)

    ids = []
    for entry in entries:
        title, fields = entry["title"], entry["fields"]
        label = f"entry {title!r}"
        if not title:
            problems.append("entry has an empty title")
        unknown = sorted(set(fields) - RECORD_FIELDS)
        if unknown:
            problems.append(f"{label}: unknown field(s) {unknown}")
        missing = sorted(REQUIRED_RECORD_FIELDS - set(fields))
        if missing:
            problems.append(f"{label}: missing required field(s) {missing}")
        empty = sorted(key for key, value in fields.items() if key in RECORD_FIELDS and not value)
        if empty:
            problems.append(f"{label}: empty field(s) {empty}")
        saved = fields.get("saved")
        if saved and not SAVED_RE.fullmatch(saved):
            problems.append(f"{label}: saved must be UTC YYYY-MM-DDTHH:MM:SSZ")
        sid = fields.get("session_id")
        if sid:
            ids.append(sid)

    duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
    if duplicates:
        problems.append(f"duplicate session id(s) {duplicates} — one entry per session id is the rule")
    return entries, problems


def render_record(heading, entries):
    lines = [heading]
    for entry in entries:
        fields = entry["fields"]
        lines.extend(["", f"## {entry['title']}", f"- saved: {fields['saved']}",
                      f"- session_id: {fields['session_id']}"])
        if fields.get("model"):
            lines.append(f"- model: {fields['model']}")
        lines.append(f"- dir: {fields['dir']}")
    return "\n".join(lines) + "\n"


def validate_value(label, value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty with no leading or trailing whitespace")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{label} must not contain control characters")


def normalize_title(title):
    return " ".join(title.casefold().split())


def within_one_edit(a, b):
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(left != right for left, right in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = j = skipped = 0
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
        else:
            skipped += 1
            if skipped > 1:
                return False
        j += 1
    return True


def similar_titles(requested, titles):
    wanted = normalize_title(requested)
    matches = []
    for title in titles:
        if title == requested:
            continue
        candidate = normalize_title(title)
        if candidate == wanted:
            reason = "case_or_whitespace"
        elif within_one_edit(candidate, wanted):
            reason = "one_edit"
        else:
            continue
        if not any(item["title"] == title for item in matches):
            matches.append({"title": title, "reason": reason})
    return matches


def allocate_title(requested, titles):
    if requested not in titles:
        return requested
    suffix = re.compile(rf"^{re.escape(requested)} - ([0-9]+)$")
    highest = 1
    for title in titles:
        match = suffix.fullmatch(title)
        if match and int(match.group(1)) >= 2:
            highest = max(highest, int(match.group(1)))
    return f"{requested} - {highest + 1}"


@contextmanager
def record_lock(path):
    target = resolved_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RecordLocked(lock) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"pid={os.getpid()}\n")
        yield target
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def atomic_write(path, text):
    target = resolved_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if old_mode is not None:
            temporary.chmod(old_mode)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
