# Tools

Small, fast, deterministic checks. Run them on every change — they test the scripts, they need no
model budget, and they are where the real bugs were caught (a detector returning another project's
session id, a Windows path needle that could never match, a `.cmd` shim that never launched).

| Tool | What it checks |
|---|---|
| `check_invariants.py` | personal data in the tree **and in every commit**, development-environment narration, decaying numbers in docs, explicit UTF-8, shell-wrapper hygiene, `SKILL.md` frontmatter |
| `check_detector_output.py` | each detector against this machine's real stores, asserting the documented output envelope (`ok`, `heuristic`, `source`/`reason`) |
| `test_platform_helpers.py` | path matching, path comparison, record parsing, command-line arguments |
| `test_detectors.py` | what each detector returns, and what it refuses to return, against synthetic stores |

```bash
python3 tools/check_invariants.py            # add --history to scan every commit too
python3 tools/check_detector_output.py
python3 tools/test_platform_helpers.py
python3 tools/test_detectors.py
cd skills/save-session && python3 scripts/check_flow_doc.py
```

## Windows

Every command above assumes `python3`, which is the POSIX name. On Windows the interpreter is
usually `python` or `py -3` — swap that in for any command in this file or in `SKILL.md`. The
checks themselves need no other platform-specific substitution: they are plain standard-library
Python and run unmodified on Windows (CI's `windows-latest` job runs this exact set).

## The lane harness was removed

There used to be a full prompt-level harness here — seeded run directories, a launcher, a grader,
and wrappers for the Antigravity/OpenCode/Codex CLIs. It was deleted deliberately: it needed five
authenticated CLIs and hours per run, it kept generating its own bugs, and its output was a
pass-rate table that no longer belongs in the docs (the README states coverage instead).

The pass-rate table it produced no longer belongs in the docs; the README states coverage instead.
It is not kept anywhere: if `SKILL.md`'s behaviour or a detector heuristic changes enough to need
one, write a small harness for that question rather than restoring this one, whose defects are
described in the commit message.
