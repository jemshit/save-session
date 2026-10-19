#!/usr/bin/env python3
"""Check that references/decision-flow.md and SKILL.md still agree.

Three kinds of check:

1. Structural — each mermaid block declares a flowchart, has balanced quoting, and every
   node referenced on an edge is declared somewhere in that block. Catches the class of
   typo that makes GitHub render an error box instead of a diagram.

2. Coverage — a declarative list of the skill's decision points, each paired with the
   phrase that must appear in SKILL.md and the phrase that must appear in the flow doc.
   If a rule is dropped from either side, the check fails. SKILL.md stays authoritative.

3. Completeness — the part that stops this file from quietly going stale:
   - every numbered step under Mode 1 and Mode 2 in SKILL.md must be mapped to at least one
     decision point, so a newly added step fails the check until someone covers it;
   - every `## ` section in SKILL.md must be either represented by a figure or explicitly
     declared out of scope, so a newly added section cannot pass unnoticed.

Markdown emphasis, backticks and line wrapping are normalized away before matching, so a
pattern can be written the way a human reads it. This is a lint, not a proof: it verifies
that each decision point is still *mentioned* on both sides, not that the wording is
semantically equivalent.

Usage:
    python3 scripts/check_flow_doc.py            # human report (python/py -3 on Windows)
    python3 scripts/check_flow_doc.py --json     # machine-readable
Exit code 0 when every check passes, 1 otherwise.
"""
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
FLOW_MD = SKILL_DIR / "references" / "decision-flow.md"

# (id, phrase required in SKILL.md, phrase required in references/decision-flow.md)
DECISION_POINTS = [
    # detection strategy order (Figure 3)
    ("det-file-reads-first", "file reads first", "read the harness store directly"),
    ("det-no-retry-denied", "do not retry it", "do not retry it"),
    ("det-ask-fallback", "ask the user for the id", "ask - the user can read it"),
    # artifact shape
    ("entry-shape-heading", "# sessions —", "# sessions —"),
    ("entry-shape-four-fields", "exactly these four fields", "exactly the four fields"),
    ("entry-shape-create-file", "create the file with its top heading", "create the file with its top heading"),
    ("entry-shape-timeline", "file order is the timeline", "file order is the timeline"),
    # Mode 1 - save
    ("m1-user-id-wins", "wins verbatim", "use it verbatim"),
    ("m1-heuristic-confirms", "confirm or correct", "confirm or correct"),
    ("m1-authoritative-direct", "save directly", "is detection authoritative"),
    ("m1-title-required", "project title from the user", "ask - project title"),
    ("m1-title-supplied-bypass", "if the request doesn't include one", "did the request already"),
    ("m1-update-same-title", "update, don't append", "update that entry in place"),
    ("m1-ask-ignore-replace", "ignore", "ignore or replace"),
    ("m1-replace-path", "replace", "retitle the entry"),
    ("m1-ignore-path", "keep the existing entry exactly as it is", "change nothing"),
    ("m1-numbering", "highest existing suffix", "highest existing suffix"),
    ("m1-confirm-reply", "title, session id", "title, session id"),
    # Mode 2 - find and resume
    ("m2-by-title", "find_session.py --title", "find_session.py --title"),
    ("m2-listing", "--list", "--list"),
    ("m2-bare-resume", "no name at all", "no name at all"),
    ("m2-never-silent-pick", "never silently pick", "let the user choose"),
    ("m2-refuse-guess-harness", "never guess which harness", "never guess the harness"),
    ("m2-recommend-newest", "most recently saved", "mark the newest as recommended"),
    ("m2-deliver-command", "exact resume command", "print the exact resume command"),
    ("m2-cd-dir", "`dir` to `cd` into", "cd dir"),
    ("m2-never-writes", "never modifies records", "never modified"),
    ("m2-entry-loss-announced", "say so instead of silently proceeding", "say so instead of proceeding"),
    # shared invariants
    ("inv-one-file-per-harness", "one file per harness", "one record file per harness"),
    ("inv-entry-identity", "identified by its **`session_id`**", "identified by its `session_id`"),
    ("inv-never-fabricate", "never fabricate", "never fabricate"),
    ("inv-destination-order", "AGENTS_MEMORIES_DIR", "AGENTS_MEMORIES_DIR"),
    ("inv-no-secrets", "no tokens, keys, or credentials", "no secrets"),
    ("inv-ask-only-missing", "ask only for what is missing", "ask only for what is missing"),
    ("inv-resuming-belongs-to-user", "resuming belongs to the user", "resuming stays with the user"),
]

MODE1_STEPS = {
    1: ["det-file-reads-first", "m1-heuristic-confirms", "m1-authoritative-direct"],
    2: ["m1-user-id-wins", "m1-heuristic-confirms", "det-ask-fallback"],
    3: ["m1-title-required", "m1-title-supplied-bypass", "inv-ask-only-missing"],
    4: ["m1-update-same-title", "m1-ask-ignore-replace", "m1-replace-path",
        "m1-ignore-path", "m1-numbering", "entry-shape-create-file"],
    5: ["m1-confirm-reply"],
}

MODE2_STEPS = {
    1: ["m2-by-title", "m2-listing", "m2-bare-resume"],
    2: ["m2-listing", "m2-refuse-guess-harness", "det-file-reads-first"],
    3: ["m2-never-silent-pick", "m2-recommend-newest"],
    4: ["m2-never-silent-pick", "m2-deliver-command"],
    5: ["m2-cd-dir", "inv-resuming-belongs-to-user"],
}

# Every `## ` section in SKILL.md must be represented by a figure, or declared here.
OUT_OF_SCOPE = {
    "where records live": "destination order and the file list live in the invariant table and the SKILL.md harness table",
    "record format": "artifact shape, documented in the Entry shape section of this file",
    "harness reference": "lookup table; resume commands are authoritative in find_session.py",
    "detecting without a shell": "strategy order is Figure 3",
    "reference": "documentation pointers, no decisions",
    "rules": "each rule is listed in the invariants table",
    "entry identity and title handling": "Figure 1 plus the invariant table",
    "mode 1 — save": "Figure 1",
    "mode 2 — find & resume": "Figure 2",
}


def normalize(text):
    """Drop markdown emphasis/backticks and collapse whitespace so phrases match by eye."""
    text = text.replace("**", " ").replace("`", " ").replace("*", " ")
    text = re.sub(r"^\s*[-#>]+\s*", " ", text, flags=re.M)
    return re.sub(r"\s+", " ", text).lower().strip()


def strip_fences(text):
    """Remove fenced code blocks so headings/step numbers inside examples are ignored."""
    return re.sub(r"```.*?```", "", text, flags=re.S)


def mermaid_blocks(text):
    return re.findall(r"```mermaid\n(.*?)```", text, flags=re.S)


def check_structure(flow_text):
    """Structural problems that break rendering. Returns a list of error strings."""
    errors = []
    blocks = mermaid_blocks(flow_text)
    if not blocks:
        return ["no mermaid blocks found in the flow doc"]
    for i, block in enumerate(blocks, 1):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("%%")]
        if not lines:
            errors.append(f"figure {i}: empty block")
            continue
        if not re.match(r"^(flowchart|graph)\s+(TD|TB|LR|RL|BT)\b", lines[0]):
            errors.append(f"figure {i}: does not start with a flowchart declaration: {lines[0]!r}")
        declared, referenced = set(), set()
        for ln in lines[1:]:
            if ln.count('"') % 2:
                errors.append(f"figure {i}: unbalanced quotes in {ln!r}")
            for node_id in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[|\{|\(|>|\(\[)", ln):
                declared.add(node_id)
            for node_id in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*-->", ln):
                referenced.add(node_id)
            for node_id in re.findall(r"-->\s*\|[^|]*\|\s*([A-Za-z_][A-Za-z0-9_]*)", ln):
                referenced.add(node_id)
            for node_id in re.findall(r"-->\s*([A-Za-z_][A-Za-z0-9_]*)", ln):
                referenced.add(node_id)
        for node_id in sorted(referenced - declared):
            errors.append(f"figure {i}: node {node_id!r} used on an edge but never declared")
        for node_id in sorted(declared):
            if not re.search(rf"\b{re.escape(node_id)}\b", block):
                errors.append(f"figure {i}: node {node_id!r} declared but never used")
    return errors


def check_coverage(skill_text, flow_text):
    skill, flow = normalize(skill_text), normalize(flow_text)
    results = []
    for rule_id, skill_phrase, flow_phrase in DECISION_POINTS:
        results.append({
            "id": rule_id,
            "skill_phrase": skill_phrase,
            "skill_ok": normalize(skill_phrase) in skill,
            "flow_phrase": flow_phrase,
            "flow_ok": normalize(flow_phrase) in flow,
        })
    return results


def numbered_steps(text, heading, next_heading):
    """Step numbers of the top-level numbered list inside a section."""
    start = text.find(heading)
    end = text.find(next_heading) if next_heading else -1
    section = text[start:end] if start != -1 and end != -1 else text[start:]
    return sorted({int(m.group(1)) for m in re.finditer(r"^(\d+)\.\s", section, flags=re.M)})


def check_completeness(skill_text, known_rule_ids):
    """Every step and every section must be accounted for. Returns a list of errors."""
    errors = []
    bare = strip_fences(skill_text)

    for heading, next_heading, mapping, label in (
        ("## Mode 1 — save", "## Mode 2", MODE1_STEPS, "Mode 1"),
        ("## Mode 2 — find & resume", "## Rules", MODE2_STEPS, "Mode 2"),
    ):
        steps = numbered_steps(bare, heading, next_heading)
        if not steps:
            errors.append(f"{label}: no numbered steps found under {heading!r}")
        for step in steps:
            if step not in mapping:
                errors.append(f"{label} step {step} is not mapped to a decision point in check_flow_doc.py")
                continue
            for rule_id in mapping[step]:
                if rule_id not in known_rule_ids:
                    errors.append(f"{label} step {step} maps to unknown decision point {rule_id!r}")
        for step in mapping:
            if step not in steps:
                errors.append(f"{label} step {step} is mapped here but no longer exists in SKILL.md")

    for heading in re.findall(r"^##\s+(.+?)\s*$", bare, flags=re.M):
        if heading.strip().lower() not in OUT_OF_SCOPE:
            errors.append(
                f"SKILL.md section {heading!r} is neither represented by a figure nor declared "
                f"out of scope in check_flow_doc.py"
            )
    for heading in OUT_OF_SCOPE:
        if heading not in {h.strip().lower() for h in re.findall(r"^##\s+(.+?)\s*$", bare, flags=re.M)}:
            errors.append(f"declared section {heading!r} no longer exists in SKILL.md")
    return errors


def main():
    for path in (SKILL_MD, FLOW_MD):
        if not path.exists():
            sys.exit(f"missing {path}")
    skill_text, flow_text = SKILL_MD.read_text(encoding="utf-8"), FLOW_MD.read_text(encoding="utf-8")

    structural = check_structure(flow_text)
    coverage = check_coverage(skill_text, flow_text)
    completeness = check_completeness(skill_text, {c[0] for c in DECISION_POINTS})
    failed = [c for c in coverage if not (c["skill_ok"] and c["flow_ok"])]
    ok = not structural and not failed and not completeness

    if "--json" in sys.argv:
        print(json.dumps({
            "ok": ok,
            "structural_errors": structural,
            "completeness_errors": completeness,
            "coverage": coverage,
        }, indent=2))
        sys.exit(0 if ok else 1)

    steps_mapped = sum(len(v) for v in MODE1_STEPS.values()) + sum(len(v) for v in MODE2_STEPS.values())
    print(f"{SKILL_MD.name} <-> {FLOW_MD.name}")
    print(f"  mermaid figures:  {len(mermaid_blocks(flow_text))}")
    print(f"  decision points:  {len(coverage)}")
    print(f"  mapped steps:     {len(MODE1_STEPS)} + {len(MODE2_STEPS)} (Mode 1 + Mode 2), {steps_mapped} mappings")
    print(f"  sections declared out of scope: {len(OUT_OF_SCOPE)}")
    if structural:
        print("\nSTRUCTURAL ERRORS")
        for e in structural:
            print(f"  - {e}")
    if failed:
        print("\nDRIFT (a decision point is documented on one side only)")
        for c in failed:
            missing = []
            if not c["skill_ok"]:
                missing.append(f"SKILL.md is missing {c['skill_phrase']!r}")
            if not c["flow_ok"]:
                missing.append(f"decision-flow.md is missing {c['flow_phrase']!r}")
            print(f"  - {c['id']}: " + "; ".join(missing))
    if completeness:
        print("\nCOMPLETENESS (a step or section is unaccounted for)")
        for e in completeness:
            print(f"  - {e}")
    print("\n" + ("OK - the flow doc matches SKILL.md" if ok else "FAIL - see above"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
