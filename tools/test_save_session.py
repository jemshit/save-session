#!/usr/bin/env python3
"""Regression tests for deterministic ledger saves and exact-id lookup."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "save-session" / "scripts"
SAVE = SCRIPTS / "save_session.py"
FIND = SCRIPTS / "find_session.py"


def load(name, filename):
    scripts = str(SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = load("record_store_for_tests", "record_store.py")
save_module = load("save_session_for_tests", "save_session.py")


class SaveSession(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ss-save-")).resolve()
        self.sessions = self.root / "sessions"
        self.project = self.root / "project"
        self.project.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def run_save(self, sid="sid-one", title="Project", *extra, env=None):
        command = [sys.executable, str(SAVE), "--harness", "pi", "--title", title,
                   "--sessions-dir", str(self.sessions)]
        if sid is not None:
            command.extend(["--id", sid])
        command.extend(extra)
        isolated = {**os.environ, "PI_SESSION_ID": "", "PI_MODEL": "", "PI_PROVIDER": ""}
        if env:
            isolated.update(env)
        out = subprocess.run(command, cwd=self.project, env=isolated, capture_output=True,
                             text=True, encoding="utf-8", errors="strict")
        lines = out.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, out.stdout + out.stderr)
        return out, json.loads(lines[0])

    def record_bytes(self):
        return (self.sessions / "pi.md").read_bytes()

    def test_creates_missing_file_with_compact_json(self):
        out, payload = self.run_save()
        self.assertEqual(out.returncode, 0)
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["title"], "Project")
        text = self.record_bytes().decode("utf-8")
        self.assertTrue(text.startswith("# Sessions — Pi\n\n## Project\n"))
        self.assertIn("- session_id: sid-one\n", text)
        self.assertIn(f"- dir: {self.project}\n", text)

    def test_same_id_and_title_updates_in_place_and_preserves_known_model(self):
        self.run_save()
        path = self.sessions / "pi.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("- dir:", "- model: provider/known\n- dir:"), encoding="utf-8")
        out, payload = self.run_save()
        self.assertEqual(out.returncode, 0)
        self.assertEqual(payload["status"], "updated")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Project"), 1)
        self.assertIn("- model: provider/known", text)

    def test_exact_title_collision_gets_next_suffix(self):
        titles = []
        for number in range(1, 12):
            out, payload = self.run_save(f"sid-{number}", "Project")
            self.assertEqual(out.returncode, 0)
            titles.append(payload["title"])
        self.assertEqual(titles, ["Project"] + [f"Project - {number}"
                                                 for number in range(2, 12)])
        self.assertEqual(len(set(titles)), len(titles))

    def test_different_title_for_same_id_requires_explicit_choice_without_writing(self):
        self.run_save("sid-one", "Old title")
        before = self.record_bytes()
        out, payload = self.run_save("sid-one", "New title")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(payload["reason"], "session_has_different_title")
        self.assertEqual(self.record_bytes(), before)

    def test_ignore_and_replace_are_explicit(self):
        self.run_save("sid-one", "Old title")
        before = self.record_bytes()
        out, payload = self.run_save("sid-one", "New title", "--ignore-existing")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(payload["status"], "ignored")
        self.assertEqual(self.record_bytes(), before)
        out, payload = self.run_save("sid-one", "New title", "--replace-title")
        self.assertEqual(payload["status"], "replaced")
        self.assertIn("## New title", self.record_bytes().decode("utf-8"))
        self.assertNotIn("## Old title", self.record_bytes().decode("utf-8"))

    def test_replace_obeys_exact_title_numbering(self):
        self.run_save("sid-one", "First")
        self.run_save("sid-two", "Wanted")
        _, payload = self.run_save("sid-one", "Wanted", "--replace-title")
        self.assertEqual(payload["title"], "Wanted - 2")

    def test_replace_still_stops_for_a_similar_other_title(self):
        self.run_save("sid-one", "First")
        self.run_save("sid-two", "data structure")
        before = self.record_bytes()
        out, payload = self.run_save("sid-one", "data structures", "--replace-title")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(payload["reason"], "similar_title")
        self.assertEqual(self.record_bytes(), before)
        out, payload = self.run_save("sid-one", "data structures", "--replace-title",
                                     "--accept-similar")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(payload["status"], "replaced")

    def test_one_edit_title_requires_confirmation_then_can_be_accepted(self):
        self.run_save("sid-one", "data structure")
        before = self.record_bytes()
        out, payload = self.run_save("sid-two", "data structures")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(payload["reason"], "similar_title")
        self.assertEqual(payload["similar_titles"][0]["reason"], "one_edit")
        self.assertEqual(self.record_bytes(), before)
        out, payload = self.run_save("sid-two", "data structures", "--accept-similar")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(payload["title"], "data structures")

    def test_case_or_whitespace_only_title_requires_confirmation(self):
        self.run_save("sid-one", "Data Structure")
        out, payload = self.run_save("sid-two", "data  structure")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(payload["similar_titles"][0]["reason"], "case_or_whitespace")

    def test_malformed_record_is_never_modified(self):
        self.sessions.mkdir()
        path = self.sessions / "pi.md"
        path.write_text("# Sessions — Pi\n\n## broken\n- session_id: sid-old\n", encoding="utf-8")
        before = path.read_bytes()
        out, payload = self.run_save()
        self.assertEqual(out.returncode, 1)
        self.assertEqual(payload["reason"], "malformed_record")
        self.assertEqual(path.read_bytes(), before)

    def test_existing_lock_fails_loudly_without_writing(self):
        self.sessions.mkdir()
        lock = self.sessions / "pi.md.lock"
        lock.write_text("pid=fixture\n", encoding="utf-8")
        out, payload = self.run_save()
        self.assertEqual(out.returncode, 1)
        self.assertEqual(payload["reason"], "record_locked")
        self.assertFalse((self.sessions / "pi.md").exists())
        self.assertTrue(lock.exists())

    def test_path_resolution_failure_uses_the_json_error_contract(self):
        metadata = {"ok": True, "heuristic": False, "session_id": "sid-one",
                    "source": "fixture", "model": None}
        output = io.StringIO()
        with mock.patch.object(save_module, "detect_metadata", return_value=metadata), \
                mock.patch.object(save_module.store, "resolved_target",
                                  side_effect=RuntimeError("fixture symlink loop")), \
                contextlib.redirect_stdout(output):
            code = save_module.main(["--harness", "pi", "--id", "sid-one",
                                     "--title", "Project", "--sessions-dir",
                                     str(self.sessions)])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "record_io_failed")
        self.assertIn("RuntimeError", payload["detail"])

    def test_unicode_title_round_trips_and_control_character_is_rejected(self):
        out, payload = self.run_save("sid-unicode", "Résumé 東京")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(payload["title"], "Résumé 東京")
        self.assertIn("Résumé 東京", self.record_bytes().decode("utf-8"))
        out, payload = self.run_save("sid-bad", "bad\ntitle")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(payload["reason"], "invalid_argument")

    def test_choice_flags_fail_when_no_conflict_exists(self):
        out, payload = self.run_save("sid-one", "Project", "--replace-title")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(payload["reason"], "choice_not_applicable")
        self.assertFalse((self.sessions / "pi.md").exists())

    def test_argument_errors_use_the_json_contract(self):
        out = subprocess.run(
            [sys.executable, str(SAVE), "--harness", "not-a-harness", "--title", "Project"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout)["reason"], "invalid_argument")
        self.assertEqual(out.stderr, "")

    def test_authoritative_pi_environment_can_save_without_explicit_id(self):
        out, payload = self.run_save(None, "Project", env={"PI_SESSION_ID": "sid-env"})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(payload["session_id"], "sid-env")

    def test_heuristic_detection_requires_confirmation(self):
        original = save_module.detect_metadata
        save_module.detect_metadata = lambda harness, sid: {
            "ok": True, "heuristic": True, "session_id": "sid-detected",
            "source": "fixture store", "model": None,
        }
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = save_module.main(["--harness", "codex", "--title", "Project",
                                         "--sessions-dir", str(self.sessions)])
        finally:
            save_module.detect_metadata = original
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason"], "confirm_detected_session")
        self.assertFalse((self.sessions / "codex.md").exists())

    def test_writer_uses_the_right_file_and_heading_for_every_harness(self):
        original = save_module.detect_metadata
        save_module.detect_metadata = lambda harness, sid: {
            "ok": True, "heuristic": False, "session_id": sid,
            "source": "user-provided", "model": None, "validated": False,
        }
        try:
            for harness, (filename, heading) in store.HARNESSES.items():
                with self.subTest(harness=harness):
                    base = self.root / harness
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = save_module.main([
                            "--harness", harness, "--id", f"sid-{harness}",
                            "--title", "Project", "--sessions-dir", str(base),
                        ])
                    self.assertEqual(code, 0, output.getvalue())
                    self.assertTrue((base / filename).read_text(encoding="utf-8").startswith(heading))
        finally:
            save_module.detect_metadata = original

    @unittest.skipIf(os.name == "nt", "creating symlinks normally needs elevated Windows rights")
    def test_symlinked_record_updates_its_target_without_replacing_the_link(self):
        self.sessions.mkdir()
        target = self.root / "actual-pi.md"
        target.write_text("# Sessions — Pi\n", encoding="utf-8")
        link = self.sessions / "pi.md"
        link.symlink_to(target)
        out, _ = self.run_save()
        self.assertEqual(out.returncode, 0)
        self.assertTrue(link.is_symlink())
        self.assertIn("## Project", target.read_text(encoding="utf-8"))


class StoreAndFind(unittest.TestCase):
    def test_one_edit_matching_covers_insert_delete_and_substitute_only(self):
        self.assertTrue(store.within_one_edit("structure", "structures"))
        self.assertTrue(store.within_one_edit("structures", "structure"))
        self.assertTrue(store.within_one_edit("structure", "structurf"))
        self.assertFalse(store.within_one_edit("structure", "structure"))
        self.assertFalse(store.within_one_edit("structure", "structural"))

    def test_atomic_replace_failure_keeps_original(self):
        root = Path(tempfile.mkdtemp(prefix="ss-atomic-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        path = root / "pi.md"
        path.write_text("original", encoding="utf-8")
        with mock.patch.object(store.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                store.atomic_write(path, "replacement")
        self.assertEqual(path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(root.glob("*.tmp")), [])

    def test_find_by_exact_session_id_returns_only_that_entry_as_compact_json(self):
        root = Path(tempfile.mkdtemp(prefix="ss-find-id-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / "pi.md").write_text(
            "# Sessions — Pi\n\n## one\n- saved: 2026-01-01T00:00:00Z\n"
            "- session_id: sid-one\n- dir: /one\n\n## two\n"
            "- saved: 2026-01-02T00:00:00Z\n- session_id: sid-two\n- dir: /two\n",
            encoding="utf-8",
        )
        out = subprocess.run(
            [sys.executable, str(FIND), "--session-id", "sid-two", "--sessions-dir",
             str(root), "--json"], capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(len(out.stdout.strip().splitlines()), 1)
        payload = json.loads(out.stdout)
        self.assertEqual([item["title"] for item in payload["matches"]], ["two"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
