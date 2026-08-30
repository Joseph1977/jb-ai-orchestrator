# Harness catalog: YAML frontmatter and duplicate entries

**Date:** 2026-08-30
**Branch:** `fix/frontmatter-yaml-parsing`
**Pull request:** [#2](https://github.com/Joseph1977/jb-ai-orchestrator/pull/2) → `on-going-dev`

Two independent defects in the capability catalog the model reads when choosing
which primitive to open. Neither affected execution.

## 1. Frontmatter parsing

### What changed

`parse_frontmatter_fields` in `src/app/services/harness/base.py` now hands the
frontmatter block to `yaml.safe_load` instead of scanning it line by line for
`key: value` pairs. Malformed frontmatter returns no fields rather than raising,
non-scalar values for `name`/`description` are ignored, and the existing
200-character cap and 4,096-character bounded scan are unchanged.

`first_description` had an off-by-one: `fm_end` was computed from `match.end()`,
which sits on the closing `---`, so the delimiter itself was treated as the first
substantive line. A file carrying `name` but no `description` was catalogued with
the description `---`. It now skips past the closing delimiter.

Added `tests/test_frontmatter_parsing.py`.

### Why

The previous parser split each line on the first colon and kept the remainder of
that same line. YAML block scalars put the value on the *following* lines, so
`description: >` stored the indicator character itself. Every catalog entry for a
skill written in that style — the dominant convention in both Claude and Cursor
skill files — was rendered as:

```text
- `help` (.claude/skills/help/SKILL.md) — >
```

The model chooses which primitive to open from that catalog, so it was choosing
between entries with no usable descriptions. All eight skills in the sample PM
workflow were affected, as are Cursor's own skills, which use `description: >-`.

This never broke execution. Progressive disclosure means the model reads the full
`SKILL.md` with `read_file_local` before applying it, and that path never
re-parses frontmatter — which is why the defect survived from the first commit of
the harness without being noticed.

## 2. Duplicate catalog entries

### What changed

`HarnessAdapter._discover_primitives` now seeds its `seen_paths` set from the
primitives already on the manifest, instead of starting empty. Added
`tests/test_catalog_deduplication.py`.

### Why

`_discover_primitives` is shared discovery that globs both dotted and flat
layouts. Its deduplication only ever guarded against collisions *within its own
pass*, so anything an adapter had already catalogued was appended a second time.

`ClaudeCodeAdapter.collect` enumerates skills, commands, and agents explicitly
before calling it, so all three doubled. Against the sample PM workflow the
catalog carried 16 skills and 18 commands where the workspace holds 8 and 9.

`CursorAdapter` has the same defect but a much smaller blast radius: it only
enumerates rules and agents explicitly, and `.cursor/rules/*.mdc` is not one of
the discovery patterns, so only `.cursor/agents/*.md` doubled. Most Cursor
workspaces have no agents directory, which is why this surfaced on a Claude
workspace first. Seeding from the manifest fixes both adapters, and `Generic`
never had the problem because it delegates entirely to discovery.

Where both sources describe the same path, the adapter's entry wins. That is the
existing first-wins ordering, so nothing else about the catalog changes.

## Migration note

None for either fix. No schema, API, or configuration change. `PyYAML>=6.0.0`
was already a declared dependency.

## Verification

- Full suite: 484 passed.
- The 14 frontmatter assertions cover folded and literal block scalars, chomping
  indicators, quoted and plain single-line values, numeric coercion, malformed
  YAML, non-scalar values, truncation, the name-only heading fallback, and the
  rendered catalog.
- The 3 deduplication tests fail against the previous code and cover the Claude
  and Cursor layouts plus adapter-description precedence.
- Against the sample PM workflow, all eight skills now catalogue their real
  descriptions instead of `>`, and each appears once.
