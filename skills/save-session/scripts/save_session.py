#!/usr/bin/env python3
"""Detect and safely create or update one saved-session ledger entry.

Exit 0: completed. Exit 1: error, nothing written. Exit 2: confirmation required,
nothing written. Operational results use one compact JSON object on stdout.
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import detect_session as detect
import record_store as store


def emit(payload, code=0):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"ok": False, "status": "error", "reason": "invalid_argument",
                          "detail": message}, separators=(",", ":")))
        raise SystemExit(1)


def auto_harness():
    if os.environ.get("PI_SESSION_ID"):
        return "pi"
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDECODE"):
        return "claude-code"
    if os.environ.get("OPENCODE"):
        return "opencode"
    return None


def detect_metadata(harness, session_id):
    if session_id is not None:
        result = detect.detect_by_id(harness, session_id)
    else:
        result = detect.HARNESSES[harness]()
    result.setdefault("heuristic", True)
    result.setdefault("model", None)
    result.setdefault("source", None)
    return result


def result_payload(status, harness, title, session_id, path, **extra):
    payload = {"ok": status not in {"needs_confirmation", "error"}, "status": status,
               "harness": harness, "title": title, "session_id": session_id,
               "file": str(path)}
    payload.update(extra)
    return payload


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = JSONArgumentParser(description="Safely save one coding-agent session")
    parser.add_argument("--harness", choices=sorted(store.HARNESSES))
    parser.add_argument("--id", dest="session_id")
    parser.add_argument("--title", required=True)
    parser.add_argument("--sessions-dir")
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument("--ignore-existing", action="store_true",
                         help="revalidate a different-title conflict without changing it")
    choices.add_argument("--replace-title", action="store_true")
    parser.add_argument("--accept-similar", action="store_true")
    args = parser.parse_args(argv)

    harness = args.harness or auto_harness()
    if harness is None:
        return emit({"ok": False, "status": "error", "reason": "unknown_harness",
                     "detail": "cannot tell which harness this is; pass --harness"}, 1)
    try:
        store.validate_value("title", args.title)
        if args.session_id is not None:
            store.validate_value("session id", args.session_id)
    except ValueError as exc:
        return emit({"ok": False, "status": "error", "reason": "invalid_argument",
                     "detail": str(exc)}, 1)

    metadata = detect_metadata(harness, args.session_id)
    if not metadata.get("ok"):
        return emit({"ok": False, "status": "error", "reason": "detection_failed",
                     "detail": metadata.get("reason") or "session detection failed"}, 1)
    session_id = metadata.get("session_id")
    if args.session_id is None and metadata.get("heuristic"):
        return emit({"ok": False, "status": "needs_confirmation",
                     "reason": "confirm_detected_session", "harness": harness,
                     "session_id": session_id, "source": metadata.get("source"),
                     "title": args.title,
                     "choices": ["rerun with --id after confirmation"]}, 2)
    try:
        store.validate_value("detected session id", session_id)
        store.validate_value("working directory", os.getcwd())
        if metadata.get("model"):
            store.validate_value("detected model", metadata["model"])
    except ValueError as exc:
        return emit({"ok": False, "status": "error", "reason": "invalid_detected_metadata",
                     "detail": str(exc)}, 1)

    try:
        base = (Path(os.path.expanduser(args.sessions_dir)) if args.sessions_dir
                else detect.sessions_dir())
    except (OSError, RuntimeError) as exc:
        return emit({"ok": False, "status": "error", "reason": "record_io_failed",
                     "detail": f"{type(exc).__name__}: {exc}"}, 1)
    path = store.record_path(base, harness)
    heading = store.HARNESSES[harness][1]
    try:
        with store.record_lock(path) as target:
            if target.exists():
                text = target.read_text(encoding="utf-8-sig")
                entries, problems = store.validate_record(text, heading)
                if problems:
                    return emit(result_payload("error", harness, args.title, session_id, target,
                                               reason="malformed_record", problems=problems), 1)
            else:
                entries = []

            same_id = next((entry for entry in entries
                            if entry["fields"].get("session_id") == session_id), None)
            titles = [entry["title"] for entry in entries]
            saved, directory, model = detect.utc_timestamp(), os.getcwd(), metadata.get("model")

            if same_id is not None and same_id["title"] != args.title:
                if args.ignore_existing:
                    return emit(result_payload("ignored", harness, same_id["title"], session_id,
                                               target, requested_title=args.title), 0)
                if not args.replace_title:
                    return emit(result_payload(
                        "needs_confirmation", harness, args.title, session_id, target,
                        reason="session_has_different_title", existing_title=same_id["title"],
                        choices=["ignore_existing", "replace_title"]), 2)
                other_titles = [entry["title"] for entry in entries if entry is not same_id]
                if args.title not in other_titles and not args.accept_similar:
                    similar = store.similar_titles(args.title, other_titles)
                    if similar:
                        return emit(result_payload(
                            "needs_confirmation", harness, args.title, session_id, target,
                            reason="similar_title", similar_titles=similar,
                            choices=["rerun --replace-title with an existing exact title",
                                     "rerun --replace-title --accept-similar"]), 2)
                same_id["title"] = store.allocate_title(args.title, other_titles)
                same_id["fields"].update({"saved": saved, "dir": directory})
                if model:
                    same_id["fields"]["model"] = model
                status = "replaced"
            elif same_id is not None:
                if args.ignore_existing or args.replace_title:
                    return emit(result_payload("error", harness, args.title, session_id, target,
                                               reason="choice_not_applicable"), 1)
                same_id["fields"].update({"saved": saved, "dir": directory})
                if model:
                    same_id["fields"]["model"] = model
                status = "updated"
            else:
                if args.ignore_existing or args.replace_title:
                    return emit(result_payload("error", harness, args.title, session_id, target,
                                               reason="choice_not_applicable"), 1)
                if args.title not in titles and not args.accept_similar:
                    similar = store.similar_titles(args.title, titles)
                    if similar:
                        return emit(result_payload(
                            "needs_confirmation", harness, args.title, session_id, target,
                            reason="similar_title", similar_titles=similar,
                            choices=["rerun with an existing exact title",
                                     "rerun with --accept-similar"]), 2)
                final_title = store.allocate_title(args.title, titles)
                fields = {"saved": saved, "session_id": session_id, "dir": directory}
                if model:
                    fields["model"] = model
                entries.append({"title": final_title, "fields": fields, "duplicates": []})
                status = "created"

            new_text = store.render_record(heading, entries)
            _, problems = store.validate_record(new_text, heading)
            if problems:
                return emit(result_payload("error", harness, args.title, session_id, target,
                                           reason="generated_invalid_record", problems=problems), 1)
            store.atomic_write(target, new_text)
            final = same_id["title"] if same_id is not None else entries[-1]["title"]
            extras = {"source": metadata.get("source")}
            if "validated" in metadata:
                extras["validated"] = metadata["validated"]
            if metadata.get("note"):
                extras["note"] = metadata["note"]
            return emit(result_payload(status, harness, final, session_id, target, **extras), 0)
    except store.RecordLocked as exc:
        return emit({"ok": False, "status": "error", "reason": "record_locked",
                     "lock_file": str(exc.path)}, 1)
    except (OSError, RuntimeError, UnicodeError) as exc:
        return emit({"ok": False, "status": "error", "reason": "record_io_failed",
                     "detail": f"{type(exc).__name__}: {exc}"}, 1)


if __name__ == "__main__":
    sys.exit(main())
