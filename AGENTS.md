# AGENTS.md

Standing instructions for anyone — human or agent — changing this repo. Read it before
editing. These are the rules that keep the skill trustworthy across machines, harnesses, and
contributors; CI enforces the mechanical ones (see [Checks](#checks-before-committing)).

## What this is

A cross-harness agent skill (`skills/save-session/`) that saves, lists, and resumes coding-agent
sessions by project title. It runs on Pi, Claude Code, Codex, Antigravity CLI, and OpenCode, on
Linux, macOS, and Windows.

## Invariants

**1. Everything must be OS-neutral.** This repo was written on macOS and has already shipped
macOS-only assumptions three times (a POSIX temp path, an XDG-only database location, a
subprocess call that ignored Windows' PATHEXT). Concretely:

- Paths go through `pathlib`; never build paths by string concatenation or assume `/`.
- Compare paths for equality with `normcase`/`normpath` — separators, drive-letter case, and
  trailing slashes all vary, and a mismatch here produces a *wrong answer*, not an error. Path
  comparison also has to survive symlinked prefixes (`resolve()` covers macOS `/tmp` and
  `/var`): a path recorded through a link and the physical path `os.getcwd()` reports are the
  same directory.
- Read and write text with an explicit `encoding="utf-8"`. The default is the locale codec
  (cp1252 on Windows), which raises on this project's own UTF-8 files.
- When searching a JSON store for a path, search every form it can take: JSON-escaped
  separators, non-ASCII raw vs `\u`-escaped, and both `"key":"v"` and `"key": "v"` spacing.
  A miss here silently selects the wrong session.
- Shell code assumes POSIX, so if any is ever added it belongs in its own `.sh` file, LF-only
  and executable (`.gitattributes` and CI enforce both). The CLI wrappers that used to sit beside
  the lane harness were removed with it.
- Never document a platform-specific command without its equivalent (see the Windows section
  of `tools/README.md`).

**2. No personal data, ever.** The skill exists to move session ids around, which makes it easy
to paste a real one into a doc or example. Never commit:

- home directories with a real username (`/Users/you`, `C:\Users\you`, `/home/you` are fine as
  placeholders — a real name is not)
- session ids that look real: only registered placeholders are allowed (see `PLACEHOLDER_IDS` in
  `tools/check_invariants.py`)
- email addresses, temp paths, tokens, or credentials

`tools/check_invariants.py` fails on all of these, including ids that appear only inside prose.

**3. No development-environment narration — but coverage caveats are required.** Notes about the
machine this was built on, or which CLI happened to be signed in while working on it, are history
and not documentation. What *is* required is the other half: any matrix or coverage claim must
state what was measured and what blocked the rest, per harness, because an empty cell with no
reason reads as "not applicable" when it usually means "not run". `tools/check_invariants.py`
bans the narrative phrasings and the README's matrix carries the caveats.

**4. `SKILL.md` is authoritative; `references/decision-flow.md` is derived.** The figures must
not drift from the rules, so `scripts/check_flow_doc.py` verifies every decision point appears
in both files and that every numbered step and section of `SKILL.md` is either covered by a
figure or explicitly declared out of scope. Edit both together.

**5. The installed skill directory stays lean.** `tools/` lives at the repo root, never inside
`skills/save-session/`, because installing the skill copies that whole directory. The Agent Skills
spec also wants `SKILL.md` under 500 lines with detail in `references/` and `scripts/`.

**6. Keep the never-guess contract.** Detection reports `{"ok": false, "reason": ...}` with a
non-zero exit status when it cannot be certain, and the agent asks instead of guessing. A wrong
session id is worse than an admitted gap. New code that finds ids or paths must follow the same
shape: a confident-looking wrong answer is the one outcome we design against.

**7. A silent wrong answer is the worst failure mode.** Prefer loud failure to plausible output.
Bugs found so far in this repo were mostly of that kind — the newest-file heuristic picking
another project's session, an empty `resume` command that read like a valid one, a locked record
file reported as empty. When a check cannot be completed, say so in the output.

**8. Platform-sensitive logic needs a unit test.** Add tests to `tools/test_platform_helpers.py`
(paths, comparison, record parsing, arguments) or `tools/test_detectors.py` (detector behaviour
against synthetic stores, run in a subprocess), and make them assert the *failure* behaviour too —
a sibling project must not match, an unreadable file must not look empty, another project's
session must not be returned as this one.

**8b. There is no lane harness, on purpose.** A prompt-level harness used to live here: seeded run
directories, a launcher, a grader, and CLI wrappers. It needed five authenticated CLIs and hours per
run, it kept generating bugs of its own, and its only output was a pass-rate table that
documentation should not carry. It is not kept anywhere. If a detector heuristic or `SKILL.md`'s
behaviour changes enough to need one, write a small harness for that question rather than restoring
the old one, whose defects are described in the commit message.

**9. Behaviour changes mean doc changes.** Changing what the skill does? Update the affected
`SKILL.md` rules and, when decision logic moves, `references/decision-flow.md` —
`scripts/check_flow_doc.py` fails when the two disagree. Adding a harness means updating the
`SKILL.md` table *and* `scripts/detect_session.py` together: the script is the authority and the
table is the no-shell fallback.

**10. Documents don't carry numbers that decay.** No pass rates, percentages, or tallies of
tests and flows in documentation. They describe the moment they were written: the matrix that used
to sit in the README was stale within hours, and at one point quoted results that were invalid
because the lanes could read the rubric. State what is durable — which harnesses are implemented,
which have been exercised end-to-end, what is untested — and let `tools/README.md` point at the
commands that produce numbers on demand. `tools/check_invariants.py` fails on score-shaped and
percentage-shaped text outside code fences.

## Versions

The skill has one `X.Y.Z` version, declared in four places that must agree: `metadata.version` in
`SKILL.md`, `package.json`, `.claude-plugin/plugin.json`, and the marketplace entry. Claude Code
uses `plugin.json`'s version first when deciding whether a marketplace plugin changed, so a bump
that misses one place can silently leave users on the old copy.

Git release tags are `vX.Y.Z`. Ordinary invariant checks validate the four declarations without
requiring a tag, so a version-bump commit and pull request can pass before release. On the release
commit, create the tag and run `python3 tools/check_invariants.py --release`; release mode requires
the matching tag to exist and point at `HEAD`.

Bump rules cover the skill's user-visible contract: saved records, detector output, flags, harness
names, resume commands, and documented behaviour.

- **patch** — compatible fixes, wording, docs, or internal robustness
- **minor** — backward-compatible features such as a mode, harness, or flag
- **major** — incompatible changes, including records becoming unreadable or ambiguous

## Checks before committing

```bash
python3 tools/check_invariants.py          # content, history, docs, frontmatter
python3 tools/test_platform_helpers.py     # path matching, path comparison, record parsing, arguments
python3 tools/test_detectors.py            # what each detector returns, and what it refuses to return
python3 tools/check_detector_output.py     # detector output contract against this machine's stores
cd skills/save-session && python3 scripts/check_flow_doc.py
```

CI runs these on Linux, macOS, and Windows, and separately verifies that the real installer can
parse the skill (`npx skills add . --list`) and that the plugin manifests validate strictly. A green
Windows job is the evidence that the OS-neutrality rules above are actually holding — reasoning is
not enough, since this project has been wrong about Windows three times.

## Layout

```
skills/save-session/     the skill: SKILL.md, references/, scripts/, agents/openai.yaml
tools/                   small checks and tests, kept out of the installed skill
```

`tools/README.md` lists the checks that remain, and says why the lane harness was removed.

## Loading these rules in Claude Code

Codex and Pi read `AGENTS.md` natively. Claude Code reads `CLAUDE.md`, so if you work in this
repo with Claude Code, create the link once:

```bash
ln -s AGENTS.md CLAUDE.md
```

That link is deliberately not committed: a `CLAUDE.md` at the repo root is also the plugin root,
and `claude plugin validate --strict` warns that plugin packaging ignores it — which would fail
the CI gate that protects the manifests users actually install. One canonical instruction file
(`AGENTS.md`) is also one less thing to keep in sync.
