#!/usr/bin/env python3
"""Check the repo's standing invariants. Fails loudly instead of relying on review.

The invariants, and why each exists:

1. No personal paths.  Machine-specific home directories leaked into examples once
   (`/Users/<you>/Workspaces/...`), which is both noise for readers and personal data.
2. No real session ids.  The skill exists to move session ids around, so ids are easy to
   paste into docs and tests by accident. Only clearly synthetic placeholders are allowed
   (see PLACEHOLDER_IDS).
3. No development-environment narration.  Notes like "quota was exhausted on the dev
   machine" are history, not documentation: they date the repo and expose the author's
   environment. Say what is true of the software instead.
4. UTF-8 is explicit.  `read_text()` without `encoding=` uses the platform locale codec
   (cp1252 on Windows) and raises on these UTF-8 files.
5. Shell wrappers stay LF-only and executable.  A CRLF checkout breaks their shebang; a
   cleared executable bit breaks the runner.
6. SKILL.md frontmatter follows the Agent Skills spec, including the silent
   installer-breaker: an unquoted colon in the description makes YAML parse it as a nested
   mapping, and installers then skip the file entirely while the listing still renders.
7. SKILL.md stays under 500 lines (spec guidance) with detail in references/ and scripts/.

Usage:
    python3 tools/check_invariants.py              # working tree
    python3 tools/check_invariants.py --history    # also every commit's patch
    python3 tools/check_invariants.py --release    # also require vX.Y.Z to tag HEAD
Exit code 0 when clean, 1 when any invariant is violated.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "save-session"
SKILL_MD = SKILL_DIR / "SKILL.md"
LAYOUT_REF = SKILL_DIR / "references" / "decision-flow.md"

TEXT_EXTENSIONS = {".md", ".py", ".sh", ".json", ".yaml", ".yml", ".txt", ".jsonl"}

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Opencode ids are `ses_` plus 26 characters. A lower bound plus a leading non-alphanumeric
# guard avoids matching identifiers that merely contain "ses_" (e.g. par*ses_fields*).
OPENCODE_ID_RE = re.compile(r"(?<![A-Za-z0-9])ses_[A-Za-z0-9]{10,}")

# Synthetic home-directory names used by tests and docs. Real usernames must not appear.
PLACEHOLDER_USERS = {"you", "user", "jose", "josé", "alice", "example", "name", "someone"}

# Personal data. Matches are allowed only when the captured user is a placeholder.
PII_PATTERNS = [
    (re.compile(r"/Users/([\w.-]+)"), "a macOS home directory"),
    (re.compile(r"/home/([\w.-]+)"), "a Linux home directory"),
    (re.compile(r"[A-Za-z]:[\\/]Users[\\/]([\w. -]+)"), "a Windows home directory"),
    (re.compile(r"/var/folders/\S+"), "a macOS temp path"),
    (re.compile(r"/private/tmp/\S+"), "a machine-specific temp path"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "an email address"),
]

# Development-environment *narration*, not documentation. The ban is on history about the machine
# this was built on. A coverage caveat — which harnesses were measured and what blocked the rest —
# is required, because an empty cell with no reason is worse than naming the blocker.
DEV_NOTE_PATTERNS = [
    (re.compile(r"\bdev(?:elopment)? machine\b", re.I), "mentions a development machine"),
    (re.compile(r"\bon this account\b", re.I), "refers to the author's account"),
    (re.compile(r"\bover quota\b", re.I), "narrates an account quota as history"),
    (re.compile(r"\bnot authenticated on the dev", re.I), "narrates local authentication state"),
]

SPEC_FRONTMATTER_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
SKILL_MAX_LINES = 500


class Report:
    def __init__(self):
        self.failures = []

    def fail(self, where, message):
        self.failures.append((where, message))

    def check(self, where, condition, message):
        if not condition:
            self.fail(where, message)


# Ids that are obviously synthetic and therefore allowed anywhere (docs, tests, examples).
# Register a new one here rather than loosening the rule: eval fixtures are auto-allowed, and
# the message this check prints will point contributors at this constant.
PLACEHOLDER_IDS = {
    "ses_abcdefghijklmnopqrstuvwxyz",        # same shape as a real id (ses_ + 26 chars)
    "55558888-4444-3333-2222-111111111155",  # what the removed harness supplied to its lanes
}
# An id that says what it is needs no registry entry: `ses_fixturefinance0001` is obviously not
# somebody's real session. Anything else that looks real gets reported.
PLACEHOLDER_TOKENS = ("fixture", "placeholder", "example", "harnesssupplied", "sample", "dummy")

# This file necessarily contains the literal patterns it forbids, so it is exempt from the two
# content checks. It stays exempt on purpose: the checker must be readable to be trustworthy.
POLICY_FILES = {"tools/check_invariants.py"}


def tracked_files():
    """Files git tracks, so nothing untracked or ignored is policed."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return [REPO / line for line in out.stdout.splitlines() if line.strip()]


def is_text(path):
    return path.suffix.lower() in TEXT_EXTENSIONS


def is_placeholder_id(value):
    """Clearly synthetic: registered, self-describing, or a UUID whose groups each repeat one
    character (11111111-2222-3333-4444-555555555555). Anything else looks real and is reported.
    """
    if value in PLACEHOLDER_IDS:
        return True
    if any(token in value.lower() for token in PLACEHOLDER_TOKENS):
        return True
    return bool(UUID_RE.fullmatch(value)) and all(len(set(g)) == 1 for g in value.split("-"))


def check_no_personal_data(report, files):
    for path in files:
        if not is_text(path):
            continue
        # as_posix(): relative_to() yields backslashes on Windows, which silently defeated this
        # comparison — the check failed on Windows CI for exactly that reason.
        rel = path.relative_to(REPO).as_posix()
        if rel in POLICY_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            report.fail(rel, f"unreadable: {exc}")
            continue
        for pattern, what in PII_PATTERNS:
            for match in pattern.finditer(text):
                captured = (match.group(1) if match.groups() else "").strip().lower()
                if captured and captured in PLACEHOLDER_USERS:
                    continue  # synthetic test/doc data, not a person
                line = text[:match.start()].count("\n") + 1
                report.fail(rel, f"line {line}: {what} ({match.group(0)!r}) — use a placeholder")
        for found in set(UUID_RE.findall(text)) | set(OPENCODE_ID_RE.findall(text)):
            if not is_placeholder_id(found):
                line = text[:text.index(found)].count("\n") + 1
                report.fail(rel, f"line {line}: session id {found!r} looks real — use a "
                                 f"placeholder, and register a new one in "
                                 f"tools/check_invariants.py PLACEHOLDER_IDS")


def check_no_development_notes(report, files):
    for path in files:
        if not is_text(path) or path.relative_to(REPO).as_posix() in POLICY_FILES:
            continue
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            # Tracked but absent: an inconsistency worth reporting, not crashing on.
            report.fail(rel, f"tracked but unreadable in the working tree: {exc}")
            continue
        for pattern, what in DEV_NOTE_PATTERNS:
            for match in pattern.finditer(text):
                line = text[:match.start()].count("\n") + 1
                report.fail(rel, f"line {line}: {what} ({match.group(0)!r}) — document the "
                                 f"software, not the environment it was built in")


def check_explicit_encoding(report, files):
    """Every read_text/write_text must pin encoding, or Windows uses cp1252."""
    for path in files:
        if path.suffix != ".py":
            continue
        rel = path.relative_to(REPO)
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.(read_text|write_text)\(", src):
            start, depth, k, in_str, quote = match.end(), 0, match.end(), False, ""
            while k < len(src):
                ch = src[k]
                if in_str:
                    if ch == quote and src[k - 1] != "\\":
                        in_str = False
                elif ch in "\"'":
                    in_str, quote = True, ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                k += 1
            if "encoding=" not in src[start:k]:
                line = src[:match.start()].count("\n") + 1
                report.fail(rel, f"line {line}: {match.group(1)}() without encoding= — "
                                 f"pins the platform locale codec instead of UTF-8")


def check_wrappers(report, files):
    for path in files:
        if path.suffix != ".sh":
            continue
        rel = path.relative_to(REPO)
        data = path.read_bytes()
        if b"\r\n" in data:
            report.fail(rel, "contains CRLF — a CRLF checkout breaks the shebang; "
                             ".gitattributes should pin this file to LF")
        if not data.startswith(b"#!"):
            report.fail(rel, "missing a shebang")
        elif not data.startswith(b"#!/usr/bin/env "):
            report.fail(rel, "shebang should use /usr/bin/env, which exists on more systems "
                             "than a hardcoded interpreter path")
        mode = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", "--", str(rel)],
                              capture_output=True, text=True, encoding="utf-8").stdout
        if mode and not mode.startswith("100755"):
            report.fail(rel, "not marked executable in git (chmod +x and commit the mode)")


def check_skill_frontmatter(report):
    rel = SKILL_MD.relative_to(REPO)
    text = SKILL_MD.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        report.fail(rel, "does not start with YAML frontmatter")
        return
    end = text.find("\n---", 3)
    if end == -1:
        report.fail(rel, "frontmatter is not closed with ---")
        return
    block = text[4:end]
    lines = block.splitlines()
    keys = [line.split(":", 1)[0].strip() for line in lines if line and not line.startswith((" ", "\t"))]
    report.check(rel, "name" in keys, "frontmatter is missing name")
    report.check(rel, "description" in keys, "frontmatter is missing description")
    unexpected = set(keys) - SPEC_FRONTMATTER_KEYS
    report.check(rel, not unexpected,
                 f"frontmatter has keys outside the spec: {sorted(unexpected)}")

    name = re.search(r"^name:\s*(.+)$", block, re.M)
    if name:
        value = name.group(1).strip().strip("\"'")
        report.check(rel, re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", value) is not None,
                     f"name {value!r} must be lowercase alphanumerics and single hyphens")
        report.check(rel, len(value) <= 64, "name must be at most 64 characters")
        report.check(rel, value == SKILL_DIR.name,
                     f"name {value!r} must match the directory name {SKILL_DIR.name!r}")

    desc = re.search(r"^description:\s*(.*)$", block, re.M)
    if desc:
        value = desc.group(1).strip()
        report.check(rel, 0 < len(value) <= 1024, "description must be 1-1024 characters")
        # The silent installer-breaker: an unquoted colon reads as a nested mapping and the
        # installer skips the file while the listing still renders.
        report.check(rel, ": " not in value,
                     "description contains an unquoted colon — YAML parses it as a nested "
                     "mapping and installers silently skip the skill; quote the value")


def check_skill_size(report):
    rel = SKILL_MD.relative_to(REPO)
    lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
    report.check(rel, len(lines) <= SKILL_MAX_LINES,
                 f"{len(lines)} lines — the spec recommends staying under {SKILL_MAX_LINES}, "
                 f"with detail moved into references/ or scripts/")
    report.check(report_rel(LAYOUT_REF), LAYOUT_REF.exists(),
                 "references/decision-flow.md is missing; SKILL.md points at it")


def report_rel(path):
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def check_history(report):
    """Scan every commit's patch with the same patterns.

    The working-tree checks cannot see history. Personal data that was committed once and
    removed later is still published by the clone, so it has to be caught here — before the
    repository is made public, not after.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "log", "--all", "--no-color", "--pretty=format:commit %h", "-p"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    ).stdout
    if not out.strip():
        return
    commit = "unknown"
    current_file = ""
    for line in out.splitlines():
        if line.startswith("commit "):
            commit = line.split(None, 1)[1].strip()
            continue
        if line.startswith(("+++ b/", "--- a/")):
            current_file = line[6:].strip()
            continue
        if current_file in POLICY_FILES:
            # The checker's own pattern definitions were committed too; they are not a leak.
            continue
        if line.startswith(("Author:", "Commit:", "Date:", "Signed-off-by:", "Co-authored-by:")):
            # Commit metadata legitimately carries the author's identity; the invariant is
            # about content, not authorship. The selected pretty format emits only "commit",
            # but keep this guard if that format changes later.
            continue
        for pattern, what in PII_PATTERNS:
            for match in pattern.finditer(line):
                captured = (match.group(1) if match.groups() else "").strip().lower()
                if captured in PLACEHOLDER_USERS:
                    continue
                report.fail(f"history {commit}", f"{what} ({match.group(0)!r})")
        for found in set(UUID_RE.findall(line)) | set(OPENCODE_ID_RE.findall(line)):
            if not is_placeholder_id(found):
                report.fail(f"history {commit}", f"session id {found!r} looks real")




# A document should not carry a number that decays. Pass rates describe the afternoon they were
# written, and the README's old matrix was stale within hours and once quoted results that were
# invalid because the lanes could read the rubric. State what is durable instead.
DOC_NUMBER_PATTERNS = [
    (re.compile(r"\b\d+/\d+\b"), "a pass-rate-like score"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?%"), "a percentage"),
    (re.compile(r"\b\d+\s+(?:evals|tests|unit tests|flows|harnesses)\b", re.I),
     "a tally that goes stale"),
    (re.compile(r"\ball \d+ (?:flows|harnesses)\b", re.I), "a tally that goes stale"),
]


def unfenced_lines(text):
    """(line number, line) for every line outside a fenced code block."""
    inside = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if not inside:
            yield number, line



# A document that points at a file should point at a file that exists. Dangling references are how
# AGENTS.md ended up telling readers to run a harness that had been deleted: nothing checked them.
REPO_PATH_RE = re.compile(r"`((?:tools|scripts|skills|references|evals|agents)/[A-Za-z0-9_./-]+)`")


def check_references(report, files):
    """Every repo-relative path a document mentions must resolve, at the root or in the skill."""
    for path in files:
        if path.suffix != ".md":
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel in POLICY_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # reported by the content checks
        for number, line in unfenced_lines(text):
            for match in REPO_PATH_RE.finditer(line):
                target = match.group(1)
                if (REPO / target).exists() or (SKILL_DIR / target).exists():
                    continue
                report.fail(rel, f"line {number}: refers to {target!r}, which does not exist — "
                                 f"a dangling reference reads as instructions")



# One skill version, declared in four places that must agree. Claude Code decides whether an
# update exists by comparing plugin.json's version, so a bump that misses it leaves users on the
# old copy with no error — the failure is silent, which is why this is checked rather than trusted.
VERSION_SOURCES = (
    ("skills/save-session/SKILL.md", "frontmatter"),
    ("package.json", "version"),
    (".claude-plugin/plugin.json", "version"),
    (".claude-plugin/marketplace.json", "plugins.0.version"),
)
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def read_declared_version(rel, where):
    path = REPO / rel
    if not path.exists():
        return None, f"{rel} is missing"
    text = path.read_text(encoding="utf-8", errors="ignore")
    if where == "frontmatter":
        block = text.split("---", 2)[1] if text.startswith("---") else ""
        match = re.search(r"^\s+version:\s*\"?([^\"\n]+?)\"?\s*$", block, re.M)
        return (match.group(1) if match else None), "metadata.version in frontmatter"
    try:
        node = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{rel} is not valid JSON: {exc}"
    for step in where.split("."):
        node = node[int(step)] if step.isdigit() and isinstance(node, list) else (
            node.get(step) if isinstance(node, dict) else None)
        if node is None:
            return None, f"{rel} has no {where}"
    return node, where


def check_versions(report, release=False):
    declared = {}
    for rel, where in VERSION_SOURCES:
        value, note = read_declared_version(rel, where)
        declared[rel] = value
        report.check(rel, bool(value), f"does not declare a version ({note})")
        if value and not VERSION_RE.fullmatch(str(value)):
            report.fail(rel, f"version {value!r} is not X.Y.Z")
    values = {str(v) for v in declared.values() if v}
    if len(values) > 1:
        report.fail("version", "the four declarations disagree: " +
                    ", ".join(f"{rel}={v}" for rel, v in declared.items() if v))
    if release and len(values) == 1:
        declared_version = next(iter(values))
        tag = f"v{declared_version}"
        tagged = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if tagged.returncode != 0:
            report.fail("version", f"release tag {tag!r} does not exist")
            return
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        report.check("version", tagged.stdout.strip() == head,
                     f"release tag {tag!r} does not point at HEAD")


def check_doc_numbers(report, files):
    for path in files:
        if path.suffix != ".md":
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel in POLICY_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # reported by the content checks
        for number, line in unfenced_lines(text):
            for pattern, what in DOC_NUMBER_PATTERNS:
                match = pattern.search(line)
                if match:
                    report.fail(rel, f"line {number}: {what} ({match.group(0)!r}) — state what is "
                                     f"true, and produce numbers on demand instead of pinning "
                                     f"them in a document")


def main():
    report = Report()
    files = tracked_files()
    if not files:
        sys.exit("no tracked files — run this inside the repo")

    check_no_personal_data(report, files)
    check_no_development_notes(report, files)
    check_explicit_encoding(report, files)
    check_wrappers(report, files)
    check_skill_frontmatter(report)
    check_skill_size(report)
    check_doc_numbers(report, files)
    check_versions(report, release="--release" in sys.argv)
    check_references(report, files)

    scope = f"{len(files)} tracked files"
    if "--history" in sys.argv:
        check_history(report)
        scope += " + all commits"
    if "--release" in sys.argv:
        scope += " + release tag"

    print(f"invariants: {scope} checked")
    if report.failures:
        print(f"\n{len(report.failures)} violation(s):")
        for where, message in report.failures:
            print(f"  - {where}: {message}")
        if any(where.startswith("history") for where, _ in report.failures):
            print("\n  History violations survive in every clone even after the file is fixed.")
            print("  Purge before publishing: git filter-repo --replace-text <patterns-file>,"
                  "\n  then force-push and re-clone elsewhere.")
        print("\nFAIL")
        return 1
    print("OK - personal data, dev notes, encodings, wrappers, and frontmatter all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
