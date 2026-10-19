# Decision flow

Visual companion to `SKILL.md` — **SKILL.md is authoritative**; this file is derived from
it and must be updated together. Run `scripts/check_flow_doc.py` to catch drift: it verifies
every decision point appears in both files, and that **every numbered step and every section**
of SKILL.md is either covered by a figure or explicitly declared out of scope.

Why the figures are split by **mode** rather than by operation: a flowchart answers "given
this request, what happens next?", and one request enters exactly one mode. Per-operation
diagrams would force the reader to hold state and re-enter at the right node — the exact
place models slip. Logic used by only one mode (id resolution) lives inside that mode's
figure.

The one deliberate exception is **Figure 3, detection**: it is a subroutine entered from
Mode 1 step 1, it has its own ordered fallbacks, and inlining it would make Figure 1
unreadably wide. It is marked as a subroutine rather than pretended to be a mode.

Hexagons (`ASK`) mark **interaction points** — every place the skill must stop and ask the
user rather than decide. In prose these are easy to skim past; here they are unmissable.

## Figure 0 — routing

```mermaid
flowchart TD
    REQ(["user request"]) --> WHICH{"what does the request ask for,<br/>read from the words not a keyword match"}
    WHICH -->|"save this session / remember it / bookmark it"| M1["Mode 1 - save"]
    WHICH -->|"resume X / find X / list my sessions / no name given"| M2["Mode 2 - find and resume"]
    M1 --> M1END["a record is created or updated"]
    M2 --> M2END["the user gets a command<br/>records are never modified"]
```

## Figure 1 — Mode 1, save

```mermaid
flowchart TD
    S(["Mode 1 - save"]) --> DETSUB["detection subroutine<br/>id, model, dir, saved and records_dir<br/>see Figure 3"]
    DETSUB --> QID{"does the request name a session id"}
    QID -->|"yes"| USERID["use it verbatim<br/>enrich via detect_session.py --id<br/>never substitute a detected id"]
    QID -->|"no"| QDET{"is detection authoritative<br/>pi env var means yes"}
    QDET -->|"yes"| DETID["use the detected id"]
    QDET -->|"no"| ASKID{{"ASK - confirm or correct<br/>show the detected id and its evidence<br/>working-directory/store results can be stale<br/>or ambiguous with concurrent sessions"}}
    ASKID --> DETID
    USERID --> HASTITLE
    DETID --> HASTITLE
    HASTITLE{"did the request already<br/>supply a project title"}
    HASTITLE -->|"yes"| LOOKUP
    HASTITLE -->|"no"| TITLE{{"ASK - project title<br/>required, keep the user's wording<br/>ask only for what is missing"}}
    TITLE --> LOOKUP{"is this session_id already in<br/>this harness's record file"}
    LOOKUP -->|"no"| CREATE{"does the record file exist"}
    CREATE -->|"no"| MKFILE["create the file with its top heading"]
    CREATE -->|"yes"| APPEND
    MKFILE --> APPEND{"is the requested title already<br/>used by a different session"}
    APPEND -->|"no"| NEW["append a new entry"]
    APPEND -->|"yes"| NEWN["append as My Project - N<br/>highest existing suffix plus one"]
    LOOKUP -->|"yes, same title"| UPD["update that entry in place<br/>refresh saved, model and dir"]
    LOOKUP -->|"yes, different title"| ANN{{"ASK - announce first<br/>already saved as X: ignore or replace"}}
    ANN -->|"ignore"| NOOP["change nothing<br/>confirm nothing was written"]
    ANN -->|"replace"| REPL["retitle the entry there<br/>refresh model and saved date"]
    NEW --> DONE
    NEWN --> DONE
    UPD --> DONE
    NOOP --> DONE
    REPL --> DONE(["confirm - title, session id, file,<br/>and created versus updated"])
```

## Figure 2 — Mode 2, find and resume

```mermaid
flowchart TD
    F(["Mode 2 - find and resume"]) --> SHAPE{"what shape is the request"}
    SHAPE -->|"names a project"| BYNAME["find_session.py --title NAME"]
    SHAPE -->|"wants everything listed"| LIST["find_session.py --list<br/>for the current harness"]
    SHAPE -->|"no name at all"| BARE["list the current harness first<br/>then ask which"]
    LIST --> HINT{"is the harness known<br/>from an env hint or an explicit flag"}
    BARE --> HINT
    HINT -->|"no"| ASKH{{"ASK - which harness, or use --all<br/>never guess the harness"}}
    HINT -->|"yes"| MATCH
    ASKH --> MATCH
    BYNAME --> MATCH{"how many entries matched"}
    MATCH -->|"none"| NONE(["say so plainly, and if the records<br/>look emptier than expected<br/>say so instead of proceeding<br/>never invent a session"])
    MATCH -->|"one"| PICK["take it"]
    MATCH -->|"several, same title or different ids"| ASKM{{"ASK - list every match sorted by saved,<br/>mark the newest as recommended,<br/>let the user choose"}}
    ASKM --> PICK
    PICK --> OUT(["print the exact resume command for that harness,<br/>plus the cd DIR to run it from<br/>resuming stays with the user - deliver the command"])
```

## Figure 3 — detection subroutine

Entered from Mode 1 step 1. The order matters more than the mechanics: file reads work
everywhere, a shell does not.

```mermaid
flowchart TD
    D(["need this session's id, model and dir"]) --> ENV{"does this harness expose<br/>a usable session environment value"}
    ENV -->|"yes"| TRUST{"is that value authoritative<br/>Pi yes, Claude still needs confirmation"}
    TRUST -->|"yes"| OUT
    TRUST -->|"no"| VALIDATE["validate it against the harness store when available<br/>and preserve the heuristic flag"]
    VALIDATE --> VALIDOK{"did validation succeed"}
    VALIDOK -->|"yes"| OUT
    VALIDOK -->|"no"| ASKU
    ENV -->|"no"| READ["read the harness store directly<br/>JSON and JSONL need no shell"]
    READ --> GOT{"id found"}
    GOT -->|"yes"| OUT(["detect_session.py result<br/>ok, heuristic flag, source<br/>current UTC saved and records_dir"])
    GOT -->|"no"| SHELL{"is a shell the only remaining path<br/>pi env vars, opencode sqlite"}
    SHELL -->|"no"| ASKU{{"ASK - the user can read it<br/>/session, /status,<br/>/resume picker, /sessions"}}
    SHELL -->|"yes"| RUN["run the command once"]
    RUN --> DENIED{"was the command denied"}
    DENIED -->|"yes"| NORETRY["do not retry it<br/>fall back to file reads or ask"]
    DENIED -->|"no"| OUT
    NORETRY --> ASKU
    ASKU --> SAVED(["record the id the user gave"])
```

## Entry shape (artifact, not flow)

Figures 1 and 2 read and write entries in this shape.

```markdown
# Sessions — <Harness>

## <project title>
- saved: 2026-10-16T23:40:00Z
- session_id: <id in that harness's own format>
- model: <provider/model>
- dir: <absolute working directory>
```

- Exactly the four fields: `saved` (UTC, ISO-8601), `session_id`, `model`, `dir`.
- If the file does not exist, create the file with its top heading first.
- Entries append oldest-first, so file order is the timeline; `saved` shows freshness.

## Invariants (not flow)

These hold everywhere and have no branch, so putting them in a figure would imply they are
optional somewhere. They live in `SKILL.md` under Rules and Entry identity.

| Invariant | Why it matters |
|---|---|
| One record file per harness | Session ids do not translate across harnesses; mixing them corrupts lookups |
| An entry is identified by its `session_id`, never its title | The id is what a resume command consumes; titles are only human labels |
| A session id is never fabricated | A wrong id is worse than an admitted gap — ask the user instead |
| Mode 2 never writes | Read paths must be safe to run anytime |
| Entry loss is announced | If the records look emptier than expected, say so instead of proceeding — quiet loss is the one failure saving cannot see |
| Resuming stays with the user | Deliver the command line; never spawn an interactive harness process to test it |
| Ask only for what is missing | Normally just the project title; do not interrogate for values detection can produce |
| Destination order: request-named → `AGENTS_MEMORIES_DIR` → `~/.agents/memories/sessions/` | An explicit destination must always win, or tests and project-local records break |
| No secrets in records | These files are plain text and often committed |

Harness → storage / model / resume mapping stays in the `SKILL.md` table, and the resume
commands are authoritative in `scripts/find_session.py`; they are deliberately not copied
here.
