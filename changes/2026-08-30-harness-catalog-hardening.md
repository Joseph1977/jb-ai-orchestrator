# Harness catalog hardening

**Date:** 2026-08-30
**Branch:** `fix/frontmatter-yaml-parsing`
**Pull request:** [#2](https://github.com/Joseph1977/jb-ai-orchestrator/pull/2) → `on-going-dev`

Defects and coverage gaps in the capability catalog the model reads when
choosing which primitive to open. Started as two frontmatter fixes; review
surfaced a blocker and several gaps, and the scope grew to cover them all.

## 1. Frontmatter parsing

`parse_frontmatter_fields` hands the block to `yaml.safe_load` instead of
scanning line by line for `key: value`. The previous parser split each line on
the first colon and kept the remainder of that same line, but YAML block scalars
put the value on the *following* lines, so `description: >` stored the indicator
character itself. Every catalog entry written in that style — the dominant
convention in both Claude and Cursor skill files — rendered as:

```text
- `help` (.claude/skills/help/SKILL.md) — >
```

All eight skills in the sample PM workflow were affected, as are Cursor's own
skills, which use `description: >-`.

This never broke execution: progressive disclosure means the model reads the
full `SKILL.md` before applying it, and that path never re-parses frontmatter,
which is why the defect survived from the first commit of the harness.

## 2. Duplicate catalog entries

`_discover_primitives` seeds `seen_paths` from the primitives already on the
manifest instead of starting empty. Its deduplication previously guarded only
against collisions *within its own pass*, so anything an adapter had already
catalogued was appended a second time. `ClaudeCodeAdapter` enumerates skills,
commands and agents explicitly before calling it, so all three doubled: the PM
workflow catalog carried 16 skills and 18 commands for a workspace holding 8
and 9. Where both sources describe the same path, the adapter's entry wins.

## 3. Malformed YAML could abort discovery

`except yaml.YAMLError` did not catch `RecursionError`, which is a
`RuntimeError`. Deeply nested sequences reach it at roughly **1,013**
characters — well inside the scan window — so one bad file aborted discovery for
the entire workspace, contradicting the guarantee that a malformed file only
degrades its own entry. Both are now caught.

## 4. The bounded scan read whole files

`_read_description_scan` called `path.read_text()` and sliced to 4,096
characters afterwards. Per catalog entry that happened **three times**: once for
the name, once for the description, and once more inside the description path.
`scan_primitive` now reads each file once, bounded at the file handle.

## 5. Frontmatter beyond the window returned `---`

When the closing delimiter fell outside the scan, the regex did not match and
the fallback scanned from line zero, returning the opening delimiter as the
description. Such files now report `TRUNCATED` and yield no description. An
unterminated leading HTML comment is treated the same way rather than leaking
comment text.

## 6. YAML alias expansion

`yaml.safe_load` is safe against code execution but not against alias
expansion. 289 characters of frontmatter loads in 0.0008s into 136 bytes,
because PyYAML shares alias references — but any recursive walk of the result
reaches **43 million nodes**, and `str()` on such a value is exactly that walk.
Recursive aliases are constructible too (`globs[0] is globs`).

The old parser was safe only by accident, because it skipped `list` and `dict`.
Reading `globs` and `paths` ends that accident, so scope fields are validated by
shape — a string, or a list whose elements pass `isinstance(x, str)`, capped at
64 items, never recursed, and containers never passed to `str()`. Only
whitelisted keys are retained, as scalars or immutable string tuples.

## 7. Loading policy replaces unconditional injection

`CursorAdapter` injected every `.mdc` eagerly, turning auto-attached,
agent-requested and manual rules into always-on ones, and listing each rule in
the catalog as well — paying for it twice. Claude rules under `.claude/rules/`
were not discovered at all, so standing and path-scoped instructions were
missing from both eager context and the catalog.

Adapters now translate their conventions into a vendor-neutral policy — `EAGER`,
`SCOPED`, `MODEL_DISCOVERABLE`, `EXPLICIT_ONLY` — carried on `PrimitiveRef`
alongside scope and provenance. `EXPLICIT_ONLY` entries are withheld from the
catalog rather than labelled, since a label does not stop the model selecting
them; commands are exempt because the model must know they exist to answer
`/help`. A rule whose frontmatter cannot be parsed is `EXPLICIT_ONLY`, not
eager. Plain `.md` under `.cursor/rules/` is ignored, matching Cursor.

## 8. Discovery coverage and traversal

Skill discovery only matched root-level `.cursor/skills/*/SKILL.md`. It now
covers `.agents/`, `.claude/`, `.codex/` and `.cursor/` roots, category
subfolders, and nested roots in monorepo packages. Nested `AGENTS.md` and
`CLAUDE.md` are catalogued as scoped rules, and `.claude/CLAUDE.md` is
recognized as project scope.

Traversal is a top-down walk that prunes `node_modules`, `.git`, `.venv` and
similar *before* descending, since `Path.glob("**/…")` would traverse them in
full before the results could be discarded. Order is sorted throughout, so
discovery is deterministic. A ceiling of 200 primitives per kind guards against
runaway discovery in an unfamiliar workspace.

## 9. Budgets

Root instructions were accepted up to 512,000 characters by a function whose
comment said it was "refusing silent truncation", then clipped at 24,000 by
`_assemble_eager` without a word. Raising the clip is not the fix either: a
400,000-character playbook loaded in full is a ~100,000-token system prompt.

Root instructions are now one mandatory allocation of the eager budget and fail
explicitly when they exceed it. Optional eager rules take what remains, whole or
not at all.

The rendered catalog gained a total character budget — 12,000 for the main
prompt, 4,000 for subagents — shared by max-min fair allocation. This replaces
the subagent tail truncation, which cut at a fixed offset and so always
sacrificed whichever kinds rendered last: Rules first, then Agents, regardless
of their size. A 200-per-kind ceiling still allows roughly 800 prompt entries,
so the character budget is what actually bounds the prompt.

## Migration notes

- **Breaking.** A root `AGENTS.md` / `CLAUDE.md` between the eager budget
  (24,000 characters by default) and the old 512,000 ceiling now raises
  `RootInstructionError` instead of being silently truncated. Split the file, or
  raise `HARNESS_EAGER_BUDGET_CHARS`.
- **Behavioural.** Cursor rules without `alwaysApply: true` are no longer
  injected. A workspace whose rules took effect only because we over-injected
  will behave differently. This is the change to watch after deploying.
- **New configuration.** `HARNESS_EAGER_BUDGET_CHARS`, optional, defaults to
  24,000. An unusable value is logged and ignored.
- No schema or API change: `summary()` is unchanged and the new policy, scope
  and source fields are internal. Eager primitives remain in the summary while
  dropping out of the catalog. `.cursorrules` and root instruction files now
  appear in `summary()["rules"]`, which is additive.
- No new dependency; `PyYAML>=6.0.0` was already declared.

## Verification

- Full suite: 559 passed, up from 484.
- `test_collect_cursor_manifest` asserted the old always-inject behaviour using
  a frontmatter-less rule, which is Manual under real Cursor semantics. It now
  covers both the eager and the withheld path.
- The bounded-read test asserts the **requested read size**, not a call count: a
  single `read_text()` of a 5 MB file is one call, so a call-count test would
  pass the very bug it guards against.
- External conventions were checked against Cursor's skills and rules
  documentation and Claude Code's memory documentation, not assumed.
