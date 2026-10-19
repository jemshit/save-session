#!/usr/bin/env python3
"""Detector behaviour tests: what each harness detector returns, and what it refuses to return.

These run the real script in a subprocess with a fake HOME and a fake CLI on PATH, so they
exercise the paths a unit test of a helper cannot: CLI discovery (including Windows .cmd
shims), the read-only database fallback, and the "no session for this directory" branches.

The rule under test is the repo's never-guess contract: when a detector cannot establish the
session for this directory it must report the gap, never hand over another project's session
with a label claiming otherwise. Four of these tests exist because it did exactly that.

    python3 tools/test_detectors.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DETECT = REPO / "skills" / "save-session" / "scripts" / "detect_session.py"

UUID = "11111111-2222-3333-4444-555555555555"


class DetectorCase(unittest.TestCase):
    """Runs detect_session.py against a synthetic home directory and PATH."""

    def setUp(self):
        # resolve(): on macOS /var and /tmp are symlinks, and os.getcwd() reports the physical
        # path, so an unresolved fixture directory would never match the detected cwd.
        self.home = Path(tempfile.mkdtemp(prefix="ss-home-")).resolve()
        self.bin = Path(tempfile.mkdtemp(prefix="ss-bin-")).resolve()
        self.work = Path(tempfile.mkdtemp(prefix="ss-work-")).resolve()
        self.addCleanup(lambda: [__import__("shutil").rmtree(p, ignore_errors=True)
                                 for p in (self.home, self.bin, self.work)])

    def fake_cli(self, name, stdout):
        """A CLI that prints `stdout`, in the shim form this platform actually resolves.

        On Windows an extensionless file would be picked up by shutil.which and then fail to
        start, so the Windows run gets only the .cmd shim — which is what exercises run_cli's
        cmd.exe branch.
        """
        if os.name != "nt":
            posix = self.bin / name
            posix.write_text(f"#!/bin/sh\ncat >/dev/null\necho '{stdout}'\n", encoding="utf-8")
            posix.chmod(0o755)
        (self.bin / f"{name}.cmd").write_text(f"@echo off\r\necho {stdout}\r\n",
                                              encoding="utf-8")

    def env(self, **overrides):
        env = {
            **os.environ,
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),           # Path.home() on Windows
            "LOCALAPPDATA": str(self.home / "AppData/Local"),
            "APPDATA": str(self.home / "AppData/Roaming"),
            # Only the fake bin dir, never the real PATH: a leaked real `opencode` on PATH
            # would get invoked against this fake HOME and could hang or answer for real,
            # rather than the "CLI absent" case many of these tests mean to exercise. Windows
            # still resolves cmd.exe via its own System32 search order regardless of PATH.
            "PATH": str(self.bin),
        }
        for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR",
                    "CLAUDE_CODE_PROJECT_DIR_NAME", "CODEX_HOME", "OPENCODE", "PI_SESSION_ID"):
            env.pop(key, None)
        env.update(overrides)
        return env

    def detect(self, harness, **env):
        out = subprocess.run(
            [sys.executable, str(DETECT), "--harness", harness],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            cwd=self.work, env=self.env(**env),
        )
        self.assertTrue(out.stdout.strip(),
                        f"detector printed nothing (exit {out.returncode}); stderr: {out.stderr[:400]}")
        result = json.loads(out.stdout)
        self.assertIn("source" if result.get("ok") else "reason", result,
                      f"payload lacks source/reason: {result}")
        return result

    def run_detector(self, *args, **env):
        return subprocess.run(
            [sys.executable, str(DETECT), *args], capture_output=True, text=True,
            encoding="utf-8", errors="ignore", cwd=self.work, env=self.env(**env),
        )


class ClaudeCode(DetectorCase):
    def transcript(self, sid=UUID, cwd=None, project_name=None, models=("initial", "latest")):
        cwd = cwd or str(self.work)
        slug = project_name or __import__("re").sub(r"[^A-Za-z0-9]", "-", str(self.work))
        directory = self.home / ".claude" / "projects" / slug
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sid}.jsonl"
        lines = [json.dumps({"type": "assistant", "cwd": cwd,
                             "message": {"model": model}}) for model in models]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_transcript_for_exact_cwd_and_latest_model_is_returned(self):
        self.transcript()
        result = self.detect("claude-code")
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], UUID)
        self.assertEqual(result["model"], "latest")

    def test_slug_collision_does_not_return_another_projects_session(self):
        other = self.work.parent / (self.work.name + "-else")
        # Force a collision directly: the detector must still inspect the transcript cwd.
        slug = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(self.work))
        self.transcript(cwd=str(other), project_name=slug)
        result = self.detect("claude-code")
        self.assertFalse(result["ok"])
        self.assertNotIn("session_id", {k: v for k, v in result.items() if v})

    def test_config_and_project_directory_overrides_are_respected(self):
        config = self.home / "custom-claude"
        directory = config / "projects" / "named-project"
        directory.mkdir(parents=True)
        (directory / f"{UUID}.jsonl").write_text(
            json.dumps({"type": "assistant", "cwd": str(self.work),
                        "message": {"model": "override"}}) + "\n",
            encoding="utf-8",
        )
        result = self.detect("claude-code", CLAUDE_CONFIG_DIR=str(config),
                             CLAUDE_CODE_PROJECT_DIR_NAME="named-project")
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "override")

    def test_session_env_is_validated_and_disclosed_as_stale_prone(self):
        self.transcript()
        result = self.detect("claude-code", CLAUDE_CODE_SESSION_ID=UUID)
        self.assertTrue(result["ok"])
        self.assertTrue(result["validated"])
        self.assertIn("stale", result["note"])

    def test_session_env_survives_missing_local_store_but_is_unvalidated(self):
        result = self.detect("claude-code", CLAUDE_CODE_SESSION_ID=UUID)
        self.assertTrue(result["ok"])
        self.assertFalse(result["validated"])
        self.assertEqual(result["session_id"], UUID)

    def test_malformed_session_env_is_a_structured_failure(self):
        result = self.detect("claude-code", CLAUDE_CODE_SESSION_ID="not-a-uuid")
        self.assertFalse(result["ok"])
        self.assertIn("not a UUID", result["reason"])

    def test_duplicate_user_id_hits_are_ambiguous(self):
        for name in ("one", "two"):
            directory = self.home / ".claude/projects" / name
            directory.mkdir(parents=True)
            (directory / f"{UUID}.jsonl").write_text(
                json.dumps({"type": "assistant", "cwd": str(self.work),
                            "message": {"model": name}}) + "\n",
                encoding="utf-8",
            )
        out = self.run_detector("--harness", "claude-code", "--id", UUID)
        result = json.loads(out.stdout)
        self.assertTrue(result["ok"], "the declared id still wins")
        self.assertFalse(result["validated"])
        self.assertIn("ambiguous", result["note"])

    def test_claude_environment_auto_detects_harness(self):
        self.transcript()
        out = self.run_detector(CLAUDECODE="1")
        result = json.loads(out.stdout)
        self.assertEqual(result["harness"], "claude-code")

    def test_unrelated_model_fields_do_not_override_assistant_model(self):
        path = self.transcript(models=("right",))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "tool_result", "model": "wrong",
                                 "message": {"model": "also-wrong"}}) + "\n")
        self.assertEqual(self.detect("claude-code")["model"], "right")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_transcript_is_reported(self):
        path = self.transcript()
        path.chmod(0)
        try:
            result = self.detect("claude-code")
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable", result["reason"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_transcript_prevents_older_readable_match(self):
        readable = self.transcript()
        unreadable = self.transcript(
            sid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            project_name="unreadable-project",
        )
        os.utime(readable, (1, 1))
        unreadable.chmod(0)
        try:
            result = self.detect("claude-code")
        finally:
            unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable", result["reason"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_project_directory_is_reported(self):
        self.transcript()
        hidden = self.home / ".claude/projects/unreadable-directory"
        hidden.mkdir()
        hidden.chmod(0)
        try:
            result = self.detect("claude-code")
        finally:
            hidden.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable", result["reason"])


class Pi(DetectorCase):
    def test_provider_and_model_are_combined(self):
        result = self.detect("pi", PI_SESSION_ID=UUID,
                             PI_PROVIDER="provider", PI_MODEL="model-name")
        self.assertTrue(result["ok"])
        self.assertFalse(result["heuristic"])
        self.assertEqual(result["model"], "provider/model-name")

    def test_detector_supplies_runtime_timestamp_and_record_destination(self):
        records = self.home / "custom-records"
        before = datetime.now(timezone.utc)
        result = self.detect("pi", PI_SESSION_ID=UUID, PI_PROVIDER="p", PI_MODEL="m",
                             AGENTS_MEMORIES_DIR=str(records))
        after = datetime.now(timezone.utc)
        saved = datetime.strptime(result["saved"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self.assertLessEqual(before.replace(microsecond=0), saved)
        self.assertLessEqual(saved, after)
        self.assertEqual(result["records_dir"], str(records))


class OpenCode(DetectorCase):
    def session(self, sid, directory):
        return {"id": sid, "title": "t", "updated": 100, "directory": directory}

    def fake_opencode_with_channel_db(self, channel_db):
        """A fake `opencode` that reports a non-default db path via `debug paths db`,
        while `session list` finds nothing — forcing the SQLite fallback to need it."""
        posix = self.bin / "opencode"
        cmd = self.bin / "opencode.cmd"
        if os.name != "nt":
            posix.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                'if [ "$1" = "debug" ]; then\n'
                f'  echo "{channel_db}"\n'
                "else\n"
                "  echo '[]'\n"
                "fi\n",
                encoding="utf-8",
            )
            posix.chmod(0o755)
        cmd.write_text(
            "@echo off\r\n"
            'if "%1"=="debug" goto debugpath\r\n'
            "echo []\r\n"
            "exit /b 0\r\n"
            ":debugpath\r\n"
            f"echo {channel_db}\r\n",
            encoding="utf-8",
        )

    def test_session_for_this_directory_is_returned(self):
        self.fake_cli("opencode", json.dumps([self.session("ses_HERE", str(self.work))]))
        result = self.detect("opencode")
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "ses_HERE")
        self.assertIn("this directory", result["source"])

    def test_another_directory_session_is_not_offered_as_this_one(self):
        # The bug this test exists for: the CLI's newest session was returned even when it
        # belonged to a different project, labelled as a cwd match.
        self.fake_cli("opencode", json.dumps([self.session("ses_ELSEWHERE", "/somewhere/else")]))
        result = self.detect("opencode")
        self.assertFalse(result["ok"], f"returned {result.get('session_id')!r} instead of a gap")
        self.assertNotIn("session_id", {k: v for k, v in result.items() if v})
        self.assertIn("none for this directory", result["reason"])

    def test_directory_absent_from_cli_output_falls_through_to_a_gap(self):
        self.fake_cli("opencode", json.dumps([{"id": "ses_NODIR", "title": "t", "updated": 9}]))
        result = self.detect("opencode")
        self.assertFalse(result["ok"])

    def test_missing_cli_reports_a_gap_not_a_traceback(self):
        result = self.detect("opencode")  # nothing on the fake PATH, no database either
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)

    def test_unexpected_valid_json_shape_reports_a_structured_gap(self):
        self.fake_cli("opencode", "null")
        result = self.detect("opencode")
        self.assertFalse(result["ok"])
        self.assertIn("unexpected JSON shape", result["reason"])

    def test_unreadable_database_is_reported(self):
        database = self.home / "broken.db"
        database.write_text("not a sqlite database", encoding="utf-8")
        result = self.detect("opencode", OPENCODE_DB=str(database))
        self.assertFalse(result["ok"])
        self.assertIn("could not be read", result["reason"])

    def test_sqlite_fallback_follows_the_cli_reported_channel_path(self):
        # A release channel (stable vs. a beta/nightly build) can relocate the data directory
        # away from the hardcoded ~/.local/share default. The CLI's own `debug paths db`
        # answer must be consulted, or a session sitting only at that location is missed.
        import sqlite3
        channel_db = self.home / "channel-specific" / "opencode.db"
        channel_db.parent.mkdir(parents=True)
        con = sqlite3.connect(channel_db)
        con.execute("CREATE TABLE session_v2 (id, model, directory, time_updated)")
        con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)",
                    ("ses_CHANNEL", None, str(self.work), 1))
        con.commit()
        con.close()
        self.fake_opencode_with_channel_db(channel_db)
        result = self.detect("opencode")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["session_id"], "ses_CHANNEL")

    def test_stale_default_database_is_not_merged_with_an_authoritative_channel_db(self):
        # Once the CLI answers, its reported database is authoritative and must be used
        # exclusively. A different, unrelated database sitting at the old default location
        # (e.g. left over from another channel or an earlier install) must not have its rows
        # merged in — a stale match there is not evidence about the *current* channel's state,
        # and surfacing it anyway is exactly the guess this skill's never-guess contract rules
        # out.
        import sqlite3

        channel_db = self.home / "channel-specific" / "opencode.db"
        channel_db.parent.mkdir(parents=True)
        con = sqlite3.connect(channel_db)
        con.execute("CREATE TABLE session_v2 (id, model, directory, time_updated)")
        con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)",
                    ("ses_OTHERDIR", None, "/somewhere/else", 1))
        con.commit()
        con.close()

        default_db = self.home / ".local/share/opencode/opencode.db"
        default_db.parent.mkdir(parents=True)
        con = sqlite3.connect(default_db)
        con.execute("CREATE TABLE session_v2 (id, model, directory, time_updated)")
        con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)",
                    ("ses_STALE", None, str(self.work), 1))
        con.commit()
        con.close()

        self.fake_opencode_with_channel_db(channel_db)
        result = self.detect("opencode")
        self.assertFalse(result["ok"], f"returned {result.get('session_id')!r} instead of a gap")
        self.assertNotIn("session_id", {k: v for k, v in result.items() if v})


class Codex(DetectorCase):
    def rollout(self, sid, cwd, root=None, models=("initial", "latest")):
        d = (root or self.home / ".codex") / "sessions/2026/01/01"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        lines = [json.dumps({"type": "session_meta", "payload": {"cwd": cwd}})]
        lines += [json.dumps({"type": "turn_context", "payload": {"model": model}})
                  for model in models]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_rollout_recording_this_directory_is_returned(self):
        self.rollout(UUID, str(self.work))
        result = self.detect("codex")
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], UUID)
        self.assertEqual(result["model"], "latest")
        self.assertIn("records this directory", result["source"])

    def test_rollout_for_another_directory_is_not_relabelled(self):
        # Previously: "newest mentioning cwd" was printed even when nothing mentioned it.
        self.rollout(UUID, "/somewhere/else")
        result = self.detect("codex")
        self.assertFalse(result["ok"])
        self.assertIn("none records this directory", result["reason"])

    def test_scans_beyond_twenty_newer_rollouts(self):
        target = self.rollout(UUID, str(self.work))
        old = time.time() - 1000
        os.utime(target, (old, old))
        for number in range(21):
            sid = f"{number:08x}-2222-3333-4444-555555555555"
            path = self.rollout(sid, f"/elsewhere/{number}")
            os.utime(path, (old + number + 1, old + number + 1))
        result = self.detect("codex")
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], UUID)

    def test_codex_home_override_is_respected(self):
        custom = self.home / "custom-codex"
        self.rollout(UUID, str(self.work), root=custom)
        result = self.detect("codex", CODEX_HOME=str(custom))
        self.assertTrue(result["ok"])

    def test_unrelated_model_fields_do_not_override_turn_context(self):
        path = self.rollout(UUID, str(self.work), models=("right",))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "event_msg", "model": "wrong",
                                 "payload": {"model": "also-wrong"}}) + "\n")
        self.assertEqual(self.detect("codex")["model"], "right")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_rollout_is_reported(self):
        path = self.rollout(UUID, str(self.work))
        path.chmod(0)
        try:
            result = self.detect("codex")
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable", result["reason"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_rollout_prevents_older_readable_match(self):
        readable = self.rollout(UUID, str(self.work))
        unreadable = self.rollout("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", str(self.work))
        os.utime(readable, (1, 1))
        unreadable.chmod(0)
        try:
            result = self.detect("codex")
        finally:
            unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable", result["reason"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_rollout_directory_is_reported(self):
        self.rollout(UUID, str(self.work))
        hidden = self.home / ".codex/sessions/2026/01/02"
        hidden.mkdir(parents=True)
        hidden.chmod(0)
        try:
            result = self.detect("codex")
        finally:
            hidden.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable", result["reason"])


class Antigravity(DetectorCase):
    def cache(self, mapping):
        d = self.home / ".gemini/antigravity-cli/cache"
        d.mkdir(parents=True, exist_ok=True)
        (d / "last_conversations.json").write_text(json.dumps(mapping), encoding="utf-8")

    def test_entry_for_this_directory_is_returned(self):
        self.cache({str(self.work): "conv-HERE"})
        result = self.detect("antigravity")
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "conv-HERE")

    def test_multi_entry_cache_without_this_directory_is_not_guessed(self):
        # Previously: an arbitrary first value was returned as "(only entry)", and the most
        # recently modified conversation was returned as if it were this directory's.
        self.cache({"/somewhere/else": "conv-A", "/another/place": "conv-B"})
        result = self.detect("antigravity")
        self.assertFalse(result["ok"])
        self.assertIn("no conversation recorded for this directory", result["reason"])

    def test_single_entry_for_another_directory_is_still_not_this_session(self):
        self.cache({"/somewhere/else": "conv-ONLY"})
        result = self.detect("antigravity")
        self.assertFalse(result["ok"])

    def test_non_mapping_cache_reports_a_structured_gap(self):
        self.cache([])
        result = self.detect("antigravity")
        self.assertFalse(result["ok"])
        self.assertIn("not a path-to-session mapping", result["reason"])

    def test_unrelated_malformed_entry_does_not_block_a_good_match(self):
        # One other workspace's bad entry must not fail detection for a directory whose own
        # entry is perfectly fine.
        self.cache({str(self.work): "conv-HERE", "/other": None, "/also-bad": ""})
        result = self.detect("antigravity")
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "conv-HERE")
        self.assertIn("malformed", result["note"])

    def test_only_malformed_entries_is_still_a_reported_gap(self):
        self.cache({"/other": None})
        result = self.detect("antigravity")
        self.assertFalse(result["ok"])
        self.assertIn("malformed", result["reason"])

    def test_newer_projects_json_cache_is_reported_honestly(self):
        # Newer Antigravity CLI releases replaced last_conversations.json with projects.json
        # plus a private SQLite conversation store; guessing that schema would violate the
        # never-guess contract, so the gap must be named specifically, not reported as a
        # generic "no last_conversations.json" miss.
        d = self.home / ".gemini/antigravity-cli/cache"
        d.mkdir(parents=True, exist_ok=True)
        (d / "projects.json").write_text("{}", encoding="utf-8")
        result = self.detect("antigravity")
        self.assertFalse(result["ok"])
        self.assertIn("projects.json", result["reason"])
        self.assertNotIn("no last_conversations.json", result["reason"])


class Arguments(DetectorCase):
    def test_empty_user_id_is_a_json_failure(self):
        out = self.run_detector("--harness", "codex", "--id", "")
        self.assertEqual(out.returncode, 1)
        result = json.loads(out.stdout)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["session_id"])

    def test_control_character_user_id_is_a_json_failure(self):
        out = self.run_detector("--harness", "codex", "--id", "bad\nid")
        self.assertEqual(out.returncode, 1)
        self.assertFalse(json.loads(out.stdout)["ok"])

    def test_id_accepts_the_equals_form(self):
        # Many agent callers pass flags as --flag=value; the space-separated form alone
        # silently dropped this and fell through to auto-detection instead of erroring.
        out = self.run_detector("--harness", "codex", f"--id={UUID}")
        result = json.loads(out.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], UUID)
        self.assertTrue(result["user_provided"])

    def test_harness_accepts_the_equals_form(self):
        out = self.run_detector("--harness=codex", "--id", UUID)
        result = json.loads(out.stdout)
        self.assertEqual(result["harness"], "codex")
        self.assertEqual(result["session_id"], UUID)

    def test_id_equals_with_no_value_still_requires_one(self):
        out = self.run_detector("--harness", "codex", "--id=")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--id requires a value", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
