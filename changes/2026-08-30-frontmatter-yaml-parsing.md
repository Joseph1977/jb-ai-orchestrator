# Skill frontmatter is parsed with PyYAML

**Date:** 2026-08-30
**Branch:** `fix/frontmatter-yaml-parsing`
**Pull request:** pending → `on-going-dev`

## What changed

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

## Why

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

## Migration note

None. No schema, API, or configuration change. `PyYAML>=6.0.0` was already a
declared dependency.

## Verification

- Full suite: 481 passed.
- The 14 new assertions cover folded and literal block scalars, chomping
  indicators, quoted and plain single-line values, numeric coercion, malformed
  YAML, non-scalar values, truncation, the name-only heading fallback, and the
  rendered catalog.
- Against the sample PM workflow, all eight skills now catalogue their real
  descriptions instead of `>`.
