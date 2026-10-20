# save-session

**Stop losing track of your AI coding sessions.**

You are deep in one coding-agent session, switch tools for another task, and a week later cannot
remember which session had the useful context—or how to get back into it. Session ids are opaque,
each harness resumes differently, and using the wrong directory can open another project's history.

`save-session` gives every supported coding agent the same simple workflow: bookmark the session
you are in under a name you will remember, then find and resume it later with the exact command and
working directory handed back to you.

```text
save this session — project is "heap benchmark"     →  one bookmark written
show me my saved sessions                            →  saved work listed
resume the heap benchmark session                    →  exact command + working directory
```

## Why install it?

- **One skill, five harnesses.** Use the same requests in Pi, Claude Code, Codex, Antigravity CLI,
  and OpenCode.
- **It never guesses.** If detection is uncertain, it asks before saving or presenting a session
  as the right one.
- **No ids or flags to memorize.** Ask by project name and get the harness's native resume command.
- **Plain text on your machine.** Bookmarks are small, human-readable Markdown files. The skill
  adds no server, account, or hosted database.

## Install

For Agent Skills-compatible harnesses, including Codex and OpenCode:

```bash
npx skills add jemshit/save-session -g
```

Omit `-g` to install only for the current project.

For Claude Code as a plugin:

```bash
claude plugin marketplace add jemshit/save-session
claude plugin install save-session@save-session
```

For Pi:

```bash
pi install git:github.com/jemshit/save-session
```

## How it works

**Save** detects the current session id, model, and working directory, then stores one bookmark
under the project title you provide. If the same session already has another title, it asks whether
to keep or replace it. If another session uses the same title, it safely adds `- 2`, `- 3`, and so
on. Similar-looking titles are shown for confirmation instead of being merged automatically.

**Find and resume** searches by project title or lists the current harness's bookmarks, then returns
the exact native resume command and the directory to run it from. Finding is read-only.

Records live in one Markdown file per harness under `~/.agents/memories/sessions/` because session
ids do not translate across harnesses. Set `AGENTS_MEMORIES_DIR` to use another location.

```markdown
# Sessions — Pi

## heap benchmark
- saved: 2026-10-16T23:40:00Z
- session_id: 11111111-2222-3333-4444-555555555555
- model: provider/model-name
- dir: <absolute path to the project>
```

## Supported harnesses

| Harness | Session detection | Resume command |
|---|---|---|
| Pi | Current `PI_SESSION_ID` | `pi --session <id>` |
| Claude Code | Environment hint validated against the project transcript when available | `claude --resume <id>` |
| Codex | Local rollout matched to the current working directory | `codex resume <id>` |
| Antigravity CLI | Older conversation cache; newer installs currently require an explicit id | `agy --conversation <id>` |
| OpenCode | `opencode session list` or the local session store | `opencode --session <id>` |

## License

MIT — see [LICENSE](LICENSE).
