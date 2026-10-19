# save-session

An agent skill for **saving, listing, and resuming coding-agent sessions by project title**.

Session ids are harness-specific and easy to lose: the id lives in a log file, the resume
command differs per tool, and pasting the wrong directory silently opens the wrong history.
This skill keeps a small per-harness ledger of sessions you want to come back to, and can
list or look them up later and hand you the exact command to resume.

```
save this session — project is "heap benchmark"     →  one entry written
show me my saved sessions                            →  everything listed, newest marked
resume the heap benchmark session                    →  id + exact resume command + cd dir
```

## What it does

**Mode 1 — save.** Detects the current session id, model, and working directory, then writes
one entry under a project title you provide. If the session is already recorded under a
different title it says so and asks whether to ignore or replace, rather than silently
retitling. A user-provided id wins; otherwise detection uses harness environment values or a
session store entry tied to the current working directory. Any non-authoritative result is shown
for confirmation before writing.

**Mode 2 — find and resume.** Looks up saved sessions by title, or lists them for the current
harness, then prints the exact resume command plus the directory to run it from. Read-only:
finding never modifies a record.

Records live in one markdown file per harness under `~/.agents/memories/sessions/`
(override with `AGENTS_MEMORIES_DIR`), because session ids do not translate across harnesses:

```markdown
# Sessions — Pi

## heap benchmark
- saved: 2026-10-16T23:40:00Z
- session_id: 11111111-2222-3333-4444-555555555555
- model: provider/model-name
- dir: <absolute path to the project>
```

## Install

```bash
# any supported harness (installs to the project or, with -g, globally)
npx skills add jemshit/save-session

# Claude Code, as a plugin
claude plugin marketplace add jemshit/save-session
claude plugin install save-session@save-session

# Pi
pi install git:github.com/jemshit/save-session
```

## Supported harnesses

Detection and resume commands are implemented for five harnesses:

| Harness | Record file | Session id source | Resume |
|---|---|---|---|
| Pi | `pi.md` | `PI_SESSION_ID` (authoritative) | `pi --session <id>` |
| Claude Code | `claude-code.md` | `CLAUDE_CODE_SESSION_ID`, validated against the project transcript when available | `claude --resume <id>` |
| Codex | `codex.md` | `$CODEX_HOME/sessions/**/rollout-*-<uuid>.jsonl` with an exact working-directory match | `codex resume <id>` |
| Antigravity CLI | `antigravity.md` | `~/.gemini/antigravity-cli/cache/last_conversations.json` (older installs; newer installs moved to `projects.json` + SQLite, not yet readable here) | `agy --conversation <id>` |
| OpenCode | `opencode.md` | `opencode session list` / `session_v2` | `opencode --session <id>` |

### End-to-end coverage

Every harness has detection and resume support (see the table above); this is what has actually
been run, because untested support is a claim:

| Harness | Exercised end-to-end |
|---|---|
| Pi | save, list, resume lookup, duplicate handling, and exact-ID resume have been exercised |
| OpenCode | save, list, resume lookup, duplicate handling, and exact-ID resume have been exercised |
| Codex | real session creation, live-store detection, and exact-ID resume have been exercised; ledger save/duplicate handling remains covered by deterministic checks rather than a prompt-level run |
| Claude Code | real session creation, live-store detection, exact-ID resume, and plugin validation have been exercised; ledger save/duplicate handling remains covered by deterministic checks rather than a prompt-level run |
| Antigravity CLI | detector behavior is covered with synthetic stores; the full workflow has not been exercised end-to-end, and the cache file this skill reads is known to be stale against current installs (see `SKILL.md`) |

Anything not listed is untested rather than working.

## Development

`tools/README.md` lists the small fast checks (personal data, detector output, path handling,
CLI arguments) and `skills/save-session/scripts/check_flow_doc.py` keeps the figures in
`references/decision-flow.md` honest. There is deliberately no prompt-level eval suite: it needed
five authenticated CLIs and hours per run, kept generating its own bugs, and produced a pass-rate
table that does not belong in a README. The coverage table above states what has actually been
exercised instead.

## Layout

```
skills/save-session/
├── SKILL.md                       # the skill: modes, rules, reference tables
├── references/decision-flow.md    # the decision logic as Mermaid figures, one per mode
├── scripts/detect_session.py      # session id / model / dir detection + --check
├── scripts/find_session.py        # lookup by title, or list a harness's entries
└── scripts/check_flow_doc.py      # fails when the figures drift from SKILL.md
tools/                             # small checks and tests, kept out of the installed skill
```

## License

MIT — see [LICENSE](LICENSE).
