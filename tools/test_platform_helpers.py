#!/usr/bin/env python3
"""Unit tests for the platform-sensitive helpers in the save-session scripts.

Nothing else exercises path matching, path comparison, record parsing, or the failure branches
that only fire on another OS, so these run on every change. Run with:

    python3 -m unittest tools.test_platform_helpers -v
    # or directly:
    python3 tools/test_platform_helpers.py
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "save-session" / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


detect = load("detect_session", "detect_session.py")
find = load("find_session", "find_session.py")

WIN_CWD = r"C:\Users\José\Workspaces\proj"


class CwdNeedles(unittest.TestCase):
    """Finding a recorded working directory inside a JSON store, on any OS."""

    def test_posix_path_is_included_verbatim(self):
        self.assertIn("/home/you/proj", detect.cwd_needles("/home/you/proj"))

    def test_windows_path_offers_both_json_forms(self):
        needles = detect.cwd_needles(WIN_CWD)
        # JSON.stringify writes non-ASCII raw; json.dumps escapes it by default. Both appear
        # in the wild, so both must be searched for.
        self.assertIn(r"C:\\Users\\José\\Workspaces\\proj", needles)
        self.assertIn(r"C:\\Users\\Jos\u00e9\\Workspaces\\proj", needles)
        # and a forward-slash variant, for stores that normalize separators
        self.assertIn("C:/Users/José/Workspaces/proj", needles)

    def test_needles_are_deduplicated(self):
        needles = detect.cwd_needles("/plain/path")
        self.assertEqual(len(needles), len(set(needles)))


class CwdMatching(unittest.TestCase):
    """The anchored match that picks the right session file or transcript."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, name, cwd, spaced=False):
        # Node (all five harnesses) writes compact JSON; Python's json.dumps inserts a space
        # after the colon. Cover both, since a mismatch here selects the wrong session.
        p = self.dir / name
        separators = None if spaced else (",", ":")
        p.write_text(
            json.dumps({"type": "session_meta", "cwd": cwd}, separators=separators) + "\n",
            encoding="utf-8",
        )
        return p

    def test_matches_a_windows_shaped_transcript(self):
        p = self.write("win.jsonl", WIN_CWD)
        self.assertTrue(detect.find_cwd_in_file(p, WIN_CWD))

    def test_matches_json_written_with_a_space_after_the_colon(self):
        p = self.write("spaced.jsonl", WIN_CWD, spaced=True)
        self.assertTrue(detect.find_cwd_in_file(p, WIN_CWD))

    def test_matches_a_non_ascii_path_written_raw(self):
        p = self.write("nonascii.jsonl", "/home/you/Документы/proj")
        self.assertTrue(detect.find_cwd_in_file(p, "/home/you/Документы/proj"))

    def test_does_not_match_a_sibling_project(self):
        # "/a/b" must not select a rollout recorded for "/a/b2".
        p = self.write("sibling.jsonl", "/work/alpha/proj2")
        self.assertFalse(detect.find_cwd_in_file(p, "/work/alpha/proj"))

    def test_does_not_match_a_parent_directory(self):
        p = self.write("child.jsonl", "/work/alpha/proj/sub")
        self.assertFalse(detect.find_cwd_in_file(p, "/work/alpha/proj"))

    @unittest.skipIf(os.name == "nt", "symlink permissions vary on Windows runners")
    def test_matches_a_symlinked_prefix(self):
        physical = self.dir / "physical"
        physical.mkdir()
        linked = self.dir / "linked"
        linked.symlink_to(physical, target_is_directory=True)
        p = self.write("symlink.jsonl", str(linked))
        self.assertTrue(detect.find_cwd_in_file(p, str(physical)))

    @unittest.skipUnless(os.name == "nt", "Windows path case rules")
    def test_matches_windows_path_case_insensitively(self):
        p = self.write("case.jsonl", r"C:\Users\You\Project")
        self.assertTrue(detect.find_cwd_in_file(p, r"c:\users\you\project"))


class JsonlReadBudget(unittest.TestCase):
    """Bytes discarded past an oversized line must still count against the read limit."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ss-budget-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.original_max_line = detect.MAX_JSONL_LINE
        detect.MAX_JSONL_LINE = 50  # a small "oversized" threshold keeps the fixture tiny
        self.addCleanup(lambda: setattr(detect, "MAX_JSONL_LINE", self.original_max_line))

    def test_oversized_lines_exhaust_the_limit_before_reaching_a_later_match(self):
        # Three 201-byte oversized "lines" (no newline within the 51-byte per-line cap), then
        # a real small match line. A 250-byte limit should be exhausted while discarding the
        # second oversized line — well before the match — because each full discarded line
        # must count toward `limit`. Before the fix, only the first readline() chunk of each
        # oversized line was charged (51 of its 201 bytes), so three such lines charged only
        # 153 bytes against a 250-byte limit and the scan sailed on into the real match below.
        junk = b"x" * 200 + b"\n"
        match_line = json.dumps({"cwd": "/target"}).encode() + b"\n"
        path = self.dir / "huge.jsonl"
        path.write_bytes(junk * 3 + match_line)
        status = detect.jsonl_cwd_status(path, "/target", limit=250)
        self.assertEqual(status, "no_match")

    def test_a_match_within_budget_is_still_found(self):
        # Same oversized-line shape, but the fix must not make the scan stop short when the
        # match is genuinely within the stated budget.
        match_line = json.dumps({"cwd": "/target"}).encode() + b"\n"
        path = self.dir / "small.jsonl"
        path.write_bytes(match_line)
        status = detect.jsonl_cwd_status(path, "/target", limit=250)
        self.assertEqual(status, "match")

    def test_a_single_massive_line_does_not_read_past_the_budget(self):
        # A match-or-no-match assertion alone can't tell a bounded scan from an unbounded one
        # when the file has only one giant, newline-free "line": both would return "no_match".
        # This counts actual bytes pulled from disk to prove the scan itself stays bounded,
        # not just that the final answer happens to be right.
        limit = 300
        huge_line = b"y" * (20 * (detect.MAX_JSONL_LINE + 1))  # far longer than the budget
        path = self.dir / "massive.jsonl"
        path.write_bytes(huge_line)

        real_open = open
        counters = []

        class CountingFile:
            def __init__(self, fh):
                self._fh = fh
                self.total_read = 0

            def readline(self, size=-1):
                data = self._fh.readline(size)
                self.total_read += len(data)
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._fh.close()

        def spy_open(file, mode="r", *args, **kwargs):
            fh = real_open(file, mode, *args, **kwargs)
            if Path(file) == path:
                wrapper = CountingFile(fh)
                counters.append(wrapper)
                return wrapper
            return fh

        import builtins
        original_open = builtins.open
        builtins.open = spy_open
        try:
            status = detect.jsonl_cwd_status(path, "/irrelevant", limit=limit)
        finally:
            builtins.open = original_open

        self.assertEqual(status, "no_match")
        self.assertEqual(len(counters), 1)
        # Bounded to roughly one extra readline()'s worth of overrun past the stated limit —
        # not the full 20x oversized line, which is what an unbounded inner loop would read.
        self.assertLess(counters[0].total_read, limit + detect.MAX_JSONL_LINE + 1)


class GlobWithStatusDepth(unittest.TestCase):
    """A permission error outside what the glob pattern can actually reach must be ignored."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ss-globstatus-")).resolve()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        for path in self.root.rglob("*"):
            if path.is_dir():
                path.chmod(stat.S_IRWXU)
        shutil.rmtree(self.root, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_directory_beyond_pattern_depth_is_ignored(self):
        # "*/*.jsonl" only ever looks one level deep. A permission error inside a *grandchild*
        # directory (nested inside an unrelated sibling project) must not fail detection for
        # every project when the glob itself would never have looked there.
        good = self.root / "good-project"
        good.mkdir()
        (good / "abc.jsonl").write_text("{}", encoding="utf-8")
        nested = self.root / "other-project" / "nested"
        nested.mkdir(parents=True)
        nested.chmod(0)
        paths, error = detect.glob_with_status(self.root, "*/*.jsonl")
        self.assertIsNone(error)
        self.assertEqual([p.name for p in paths], ["abc.jsonl"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_directory_within_pattern_depth_still_fails(self):
        # A sibling project directory the pattern *would* look inside must still fail loudly:
        # it could itself hold a matching file this glob can't see.
        good = self.root / "good-project"
        good.mkdir()
        (good / "abc.jsonl").write_text("{}", encoding="utf-8")
        unreadable = self.root / "unreadable-project"
        unreadable.mkdir()
        unreadable.chmod(0)
        paths, error = detect.glob_with_status(self.root, "*/*.jsonl")
        self.assertIsNotNone(error)
        self.assertEqual(paths, [])

    def test_recursive_pattern_still_does_a_full_walk(self):
        nested = self.root / "a" / "b" / "c"
        nested.mkdir(parents=True)
        (nested / "rollout-x.jsonl").write_text("{}", encoding="utf-8")
        paths, error = detect.glob_with_status(self.root, "**/rollout-*.jsonl")
        self.assertIsNone(error)
        self.assertEqual(len(paths), 1)


class PathComparison(unittest.TestCase):
    def test_trailing_separator_and_slash_direction(self):
        self.assertTrue(detect.same_path("/a/b/", "/a/b"))
        self.assertTrue(detect.same_path("C:\\a\\b", "C:/a/b"))

    def test_case_folding_follows_the_local_filesystem(self):
        if os.name == "nt":
            self.assertTrue(detect.same_path(r"C:\Users\You", "c:/users/you"))
        else:
            self.assertFalse(detect.same_path("/a/B", "/a/b"))

    def test_rejects_different_paths(self):
        self.assertFalse(detect.same_path("/a/b", "/a/bc"))
        self.assertFalse(detect.same_path("", "/a/b"))

    def test_dir_matches_ancestors_only(self):
        self.assertTrue(detect.dir_matches("/work/alpha", "/work/alpha/proj"))
        self.assertTrue(detect.dir_matches("/work/alpha/proj", "/work/alpha/proj"))
        self.assertFalse(detect.dir_matches("/work/alpha/proj", "/work/alpha"))

    @unittest.skipIf(os.name == "nt", "symlink permissions vary on Windows runners")
    def test_dir_matches_resolves_symlinked_ancestor(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        physical = root / "physical"
        child = physical / "child"
        child.mkdir(parents=True)
        link = root / "linked"
        link.symlink_to(physical, target_is_directory=True)
        self.assertTrue(detect.dir_matches(str(link), str(child)))


class DatabaseLocations(unittest.TestCase):
    def test_xdg_data_home_is_honoured(self):
        # Isolated from opencode_cli_db_path: this test is about the XDG fallback, and must
        # not depend on whether opencode happens to be installed on the machine running it.
        with patched_cli_db_path(None), patched_env(XDG_DATA_HOME="/custom/data"):
            candidates = detect.opencode_db_candidates()
        # Compare Paths, not strings: Windows normalizes separators, and os.path.join joins
        # with the local separator, so a raw string compare is POSIX-shaped.
        self.assertEqual(candidates[0], Path("/custom/data") / "opencode" / "opencode.db")

    def test_explicit_opencode_database_wins(self):
        explicit = Path("/custom/opencode.db")
        with patched_env(OPENCODE_DB=str(explicit)):
            candidates = detect.opencode_db_candidates()
        self.assertEqual(candidates[0], explicit)
        self.assertEqual(candidates, [explicit])

    def test_default_includes_the_xdg_default(self):
        with patched_cli_db_path(None), patched_env():
            candidates = detect.opencode_db_candidates()
        expected = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        self.assertIn(expected, [Path(c) for c in candidates])

    def test_cli_reported_path_wins_outright_when_available(self):
        # v2's release channel (stable vs. a beta/nightly build) can relocate the data
        # directory, so the tool's own answer via `opencode debug paths db` must be used
        # exclusively, not merged with the hardcoded XDG/default guess — merging in a stale
        # database from a different channel could surface an outdated session as current.
        channel_path = Path("/channel-specific/opencode.db")
        with patched_cli_db_path(channel_path), patched_env():
            candidates = detect.opencode_db_candidates()
        self.assertEqual(candidates, [channel_path])

    def test_explicit_database_still_wins_over_the_cli_report(self):
        explicit = Path("/custom/opencode.db")
        with patched_cli_db_path(Path("/channel-specific/opencode.db")), \
             patched_env(OPENCODE_DB=str(explicit)):
            candidates = detect.opencode_db_candidates()
        self.assertEqual(candidates, [explicit])

    def test_explicit_database_does_not_merge_stale_default_rows(self):
        import sqlite3
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        explicit = root / "explicit.db"
        con = sqlite3.connect(explicit)
        con.execute("CREATE TABLE session_v2 (id, model, directory, time_updated)")
        con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)",
                    ("ses_explicit", None, "/right", 1))
        con.commit()
        con.close()
        with patched_env(OPENCODE_DB=str(explicit), XDG_DATA_HOME=str(root / "ignored")):
            rows = detect.opencode_db_rows()
        self.assertEqual([row[0] for row in rows], ["ses_explicit"])

    def test_stale_or_invalid_database_does_not_mask_valid_later_database(self):
        import sqlite3
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        bad, stale, good = root / "bad.db", root / "stale.db", root / "good.db"
        bad.write_text("not sqlite", encoding="utf-8")
        con = sqlite3.connect(stale)
        con.execute("CREATE TABLE session_v2 (id, model, directory, time_updated)")
        con.commit()
        con.close()
        con = sqlite3.connect(good)
        con.execute("CREATE TABLE session_v2 (id, model, directory, time_updated)")
        con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)",
                    ("ses_fixture", None, "/work", 1))
        con.commit()
        con.close()
        original = detect.opencode_db_candidates
        detect.opencode_db_candidates = lambda: [bad, stale, good]
        try:
            self.assertEqual(detect.opencode_db_rows()[0][0], "ses_fixture")
        finally:
            detect.opencode_db_candidates = original


class RecordReading(unittest.TestCase):
    """Record files: memory dir resolution, BOM tolerance, and unreadable files."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.record = self.dir / "pi.md"

    def test_memories_dir_expands_user(self):
        # Path comparison again: expanduser only rewrites the leading ~, leaving mixed
        # separators in the string, so string equality fails on Windows for equal paths.
        expected = Path(os.path.expanduser("~/.agents/memories/sessions"))
        with patched_env(AGENTS_MEMORIES_DIR="~/.agents/memories/sessions"):
            self.assertEqual(Path(detect.sessions_dir()), expected)
            self.assertEqual(Path(find.sessions_dir()), expected)

    def test_heading_sniff_survives_a_bom(self):
        self.record.write_bytes(b"\xef\xbb\xbf# Sessions \xe2\x80\x94 Pi\n\n## proj\n- session_id: x\n")
        text = self.record.read_text(encoding="utf-8-sig", errors="ignore")
        self.assertEqual(find.harness_of(self.record, text), "pi")

    def test_harness_lookup_ignores_filename_case(self):
        # A record file named Pi.md can exist on a case-insensitive filesystem.
        self.assertEqual(find.harness_of(self.dir / "Pi.md", ""), "pi")
        self.assertEqual(find.harness_of(self.dir / "OpenCode.md", ""), "opencode")

    def test_resume_is_none_rather_than_empty_for_an_unknown_harness(self):
        match = find.to_match(self.dir / "odd.md", "not-a-harness", "t", {"session_id": "abc"})
        self.assertIsNone(match["resume"])

    def test_resume_is_none_when_no_session_id_is_recorded(self):
        self.assertIsNone(find.to_match(self.record, "pi", "t", {})["resume"])

    def test_resume_is_built_for_a_known_harness(self):
        match = find.to_match(self.record, "pi", "t", {"session_id": "abc"})
        self.assertEqual(match["resume"], "pi --session abc")
        expected = {
            "claude-code": "claude --resume abc",
            "codex": "codex resume abc",
            "antigravity": "agy --conversation abc",
            "opencode": "opencode --session abc",
        }
        for harness, command in expected.items():
            with self.subTest(harness=harness):
                match = find.to_match(self.dir / f"{harness}.md", harness, "t",
                                      {"session_id": "abc"})
                self.assertEqual(match["resume"], command)

    def test_auto_harness_reads_env_hints_in_precedence_order(self):
        # Ambient harness variables belong to the developer running the tests, not this
        # fixture. Exercise the complete precedence contract in isolated environments so the
        # result is identical when the suite itself runs inside Pi, Claude Code, or OpenCode.
        with patched_env(PI_SESSION_ID="x", CLAUDECODE="1", OPENCODE="1"):
            self.assertEqual(find.auto_harness(), "pi")
        with patched_env(CLAUDECODE="1", OPENCODE="1"):
            self.assertEqual(find.auto_harness(), "claude-code")
        with patched_env(CLAUDE_CODE_SESSION_ID="x", OPENCODE="1"):
            self.assertEqual(find.auto_harness(), "claude-code")
        with patched_env(OPENCODE="1"):
            self.assertEqual(find.auto_harness(), "opencode")
        with patched_env():
            self.assertIsNone(find.auto_harness())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_record_is_reported_not_silently_empty(self):
        self.record.write_text("# Sessions — Pi\n\n## proj\n- session_id: abc\n", encoding="utf-8")
        self.record.chmod(0)
        try:
            with patched_env(AGENTS_MEMORIES_DIR=str(self.dir)):
                report = run_check_mode()
        finally:
            self.record.chmod(stat.S_IRUSR | stat.S_IWUSR)
        entry = report["files"][0]
        self.assertIn("unreadable", entry)
        self.assertEqual(entry["entries"], 0)
        self.assertFalse(report["ok"], "a record that cannot be read must not report ok")


class EntryParsing(unittest.TestCase):
    def test_parses_fields_under_each_heading(self):
        text = "# Sessions — Pi\n\n## one\n- session_id: a\n- dir: /x\n\n## two - 2\n- session_id: b\n"
        entries, problems = find.parse_entries(text)
        self.assertEqual([t for t, _ in entries], ["one", "two - 2"])
        self.assertEqual(entries[1][1]["session_id"], "b")
        self.assertEqual(problems, [])

    def test_ignores_non_field_lines(self):
        entries, _ = find.parse_entries("# Sessions — Pi\n\n## one\n- session_id: a\nnote\n")
        self.assertEqual(list(entries[0][1]), ["session_id"])

    def test_two_session_ids_under_one_heading_are_reported(self):
        # The malformed shape that silently dropped an id and made 18 evals unsatisfiable.
        text = "# Sessions — Pi\n\n## one\n- session_id: first\n- session_id: second\n"
        entries, problems = find.parse_entries(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("more than one session_id", problems[0])
        self.assertIn("first", problems[0])
        self.assertTrue(entries[0][1]["_invalid"])


class InvariantChecker(unittest.TestCase):
    """The checker itself runs on Windows, so its own path handling is a platform surface."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("inv", REPO / "tools" / "check_invariants.py")
        self.inv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.inv)

    def test_versions_are_plain_three_part_numbers(self):
        self.assertTrue(self.inv.VERSION_RE.fullmatch("0.1.0"))
        self.assertTrue(self.inv.VERSION_RE.fullmatch("12.34.56"))
        self.assertFalse(self.inv.VERSION_RE.fullmatch("v0.1.0"))
        self.assertFalse(self.inv.VERSION_RE.fullmatch("01.2.3"))
        self.assertFalse(self.inv.VERSION_RE.fullmatch("1.2.3-alpha"))

    def test_policy_file_exemption_survives_backslash_paths(self):
        # relative_to() returns "tools\\check_invariants.py" on Windows. Comparing that against
        # a forward-slash allowlist is how this check failed on Windows CI the first time.
        rel = (REPO / "tools" / "check_invariants.py").relative_to(REPO)
        self.assertIn(rel.as_posix(), self.inv.POLICY_FILES)
        self.assertIn("tools/check_invariants.py", self.inv.POLICY_FILES)

    def test_synthetic_placeholder_users_are_allowed(self):
        for name in ("you", "user", "josé"):
            self.assertIn(name, self.inv.PLACEHOLDER_USERS)

    def test_opencode_id_pattern_ignores_identifiers_containing_ses(self):
        # A real id is ses_ plus 26 chars; "parses_fields" must not be mistaken for one.
        self.assertIsNone(self.inv.OPENCODE_ID_RE.search("test_parses_fields_under_each_heading"))
        self.assertIsNotNone(self.inv.OPENCODE_ID_RE.search("ses_abcdefghijklmnopqrstuvwxyz"))



def patched_env(**overrides):
    """Context manager that sets/unsets environment variables for the duration."""
    class _Ctx:
        KEYS = ("AGENTS_MEMORIES_DIR", "XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA",
                "OPENCODE_DB", "PI_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE",
                "OPENCODE")

        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in self.KEYS}
            for k in self.KEYS:
                os.environ.pop(k, None)
            os.environ.update({k: v for k, v in overrides.items() if v is not None})
            return self

        def __exit__(self, *exc):
            for k, v in self.saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return False

    return _Ctx()


def patched_cli_db_path(value):
    """Context manager that stubs opencode_cli_db_path(), isolating a test from whatever
    opencode build (if any) is actually installed on the machine running the suite."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        original = detect.opencode_cli_db_path
        detect.opencode_cli_db_path = lambda: value
        try:
            yield
        finally:
            detect.opencode_cli_db_path = original

    return _ctx()


def run_check_mode():
    """Call detect_session's --check and return the parsed report."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            detect.check_mode()
        except SystemExit:
            pass
    return json.loads(buf.getvalue())


class Arguments(unittest.TestCase):
    """A flag that would do nothing must fail loudly, not be silently ignored."""

    def run_find_session(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "find_session.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
        )

    def test_pick_latest_with_list_is_rejected(self):
        # Silently ignoring --pick read as "the newest was used" while every entry was listed.
        out = self.run_find_session("--list", "--pick", "latest")
        self.assertNotEqual(out.returncode, 0, "--pick with --list should fail")
        self.assertIn("--pick applies to a --title lookup", out.stderr)

    def test_missing_title_and_list_is_rejected(self):
        out = self.run_find_session()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--title", out.stderr)

    def test_unknown_harness_is_rejected(self):
        out = self.run_find_session("--list", "--harness", "not-a-harness")
        self.assertNotEqual(out.returncode, 0)

    def test_duplicate_session_id_never_emits_a_resume_command(self):
        directory = Path(tempfile.mkdtemp(prefix="ss-find-corrupt-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        (directory / "pi.md").write_text(
            "# Sessions — Pi\n\n## project\n- session_id: first\n- session_id: second\n",
            encoding="utf-8",
        )
        out = self.run_find_session("--title", "project", "--sessions-dir", str(directory))
        self.assertEqual(out.returncode, 1)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["matches"], [])
        self.assertNotIn("pi --session", out.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_ledger_is_reported_without_a_traceback(self):
        directory = Path(tempfile.mkdtemp(prefix="ss-find-unreadable-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        record = directory / "pi.md"
        record.write_text("# Sessions — Pi\n", encoding="utf-8")
        record.chmod(0)
        try:
            out = self.run_find_session("--list", "--harness", "pi",
                                        "--sessions-dir", str(directory))
        finally:
            record.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(out.returncode, 1)
        self.assertIn("unreadable", out.stdout + out.stderr)
        self.assertNotIn("Traceback", out.stderr)


class CheckMode(unittest.TestCase):
    """--check is the ledger integrity command, so it must fail on a malformed entry."""

    def setUp(self):
        self.memories = Path(tempfile.mkdtemp(prefix="ss-check-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.memories, ignore_errors=True))

    def run_check(self, body):
        (self.memories / "pi.md").write_text(body, encoding="utf-8")
        env = {**os.environ, "AGENTS_MEMORIES_DIR": str(self.memories)}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "detect_session.py"), "--check"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", env=env,
        )

    def test_two_session_ids_under_one_heading_fail(self):
        out = self.run_check("# Sessions — Pi\n\n## one\n- saved: 2026-01-01T00:00:00Z\n"
                             "- session_id: aaa\n- session_id: bbb\n- dir: /work\n")
        self.assertEqual(out.returncode, 1)
        self.assertIn("duplicate field", out.stdout)

    def test_duplicate_session_ids_fail(self):
        body = ("# Sessions — Pi\n\n## one\n- saved: 2026-01-01T00:00:00Z\n"
                "- session_id: aaa\n- dir: /one\n\n## two\n- saved: 2026-01-02T00:00:00Z\n"
                "- session_id: aaa\n- dir: /two\n")
        out = self.run_check(body)
        self.assertEqual(out.returncode, 1)
        self.assertIn("duplicate session id", out.stdout)

    def test_a_well_formed_ledger_passes(self):
        body = ("# Sessions — Pi\n\n## one\n- saved: 2026-01-01T00:00:00Z\n"
                "- session_id: aaa\n- dir: /one\n\n## two\n- saved: 2026-01-02T00:00:00Z\n"
                "- session_id: bbb\n- dir: /two\n")
        out = self.run_check(body)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn("problems", out.stdout)

    def test_missing_required_fields_fail(self):
        out = self.run_check("# Sessions — Pi\n\n## broken\n- model: provider/model\n")
        self.assertEqual(out.returncode, 1)
        self.assertIn("missing required field", out.stdout)

    def test_bad_top_heading_and_timestamp_fail(self):
        body = ("# wrong\n\n## one\n- saved: yesterday\n- session_id: aaa\n- dir: /one\n")
        out = self.run_check(body)
        self.assertEqual(out.returncode, 1)
        self.assertIn("expected top heading", out.stdout)
        self.assertIn("must be UTC", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
