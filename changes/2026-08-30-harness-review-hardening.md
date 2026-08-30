# Harness review hardening: bounded reads, policy coverage, pruning, visible failures

**Date:** 2026-08-30
**Branch / PR:** `fix/frontmatter-yaml-parsing` (jb-ai-orchestrator PR #2) → `on-going-dev`

Second review round on PR #2. Six findings, all confirmed against the code
before any change was made.

## What changed

### Eager and root-instruction reads are bounded at the handle (high)

`read_text_capped` and `read_root_instructions` called `read_text()` and only
then compared the result against their caps, so the cap never protected
anything — a hostile multi-gigabyte rule or root playbook was already resident
by the time the check ran. Both now read at most `cap + 1` characters from the
file handle, which is exactly what distinguishes "at the cap" from "over it"
without a second metadata lookup.

Rule bodies also became whole-or-drop. The whole-or-drop guarantee existed in
`_assemble_eager` but the read beneath it still truncated, so an oversized rule
reached the model clipped. Oversized rules are now dropped entirely and
reported. Truncation is retained for content a clip does not invalidate, such
as a README.

### The invocation opt-out is honoured wherever primitives are enumerated (high)

The Claude adapter listed its own skills and agents with `first_description`
and no policy, so `disable-model-invocation` was ignored and every entry
defaulted to model-discoverable. Because adapter entries win during
deduplication, shared discovery never got the chance to correct it: the opt-out
was unenforceable in any workspace the Claude adapter claimed.

Both paths now go through `scan_primitive` and a shared `skill_policy` helper.

Commands are exempt, and this is now explicit rather than incidental. A slash
command is reached by name, so withholding it from the catalog would hide it
from the only mechanism that invokes it; commands stay model-discoverable
whatever their frontmatter says.

### Pruning and the primitive ceiling cover every discovery path (high)

Both guarantees only ever covered shared discovery. Adapters enumerated their
own rules, skills, commands and agents with `rglob` and a plain `append`, so a
workspace an adapter claimed was traversed without pruning and catalogued
without limit. Shared discovery's own `**` globs had already walked
`node_modules` before their results could be filtered.

Recursive discovery now uses a pruning walker, and every append goes through
one helper that enforces the per-kind ceiling and reports the first entry it
refuses. The pattern table is split into directory and filename parts so the
recursive cases can be walked rather than globbed. Ordering remains sorted, so
output is unchanged wherever the old code was already correct.

### Execute surfaces root-budget failures (medium)

Harness discovery in the execute path was wrapped in `except Exception: pass`.
An over-budget root playbook fell back to whatever eager context was already
stored and ran the segment against instructions the author had since changed —
the silent truncation the explicit-failure policy exists to prevent, with no
log line to show it had happened.

`RootInstructionError` now propagates and is returned as a
`ROOT_INSTRUCTIONS_TOO_LARGE` error, finalising the execution as failed like
the existing binding and storage errors. Every other discovery failure keeps
its fallback but is logged with a traceback. AG-UI's best-effort discovery is a
separate documented decision and is unchanged.

### Scoped instructions dropped at the ceiling are reported (medium)

Once the rule bucket filled, scoped instruction discovery broke out of its loop
with no log and no note, making a vanished nested `AGENTS.md` indistinguishable
from one that had never been written. It now uses the same ceiling-enforcing
append as everything else. Reporting was preferred over reserving capacity: a
reservation would impose an arbitrary source priority on a bucket that several
kinds of rule share.

### Comment truncation is limited to a leading comment (low)

Any unmatched `<!--` in the metadata window marked the whole scan truncated, so
a stray marker in prose below valid frontmatter discarded metadata that had
already parsed cleanly. Only a leading comment can hide metadata; a marker
anywhere else is prose.

## Migration / breaking

No schema or configuration change.

- **New error code.** Execute can now return `ROOT_INSTRUCTIONS_TOO_LARGE`
  (HTTP 400) where it previously ran with stale eager context. This is the
  intended behavioural change: the failure was always real, only invisible.
- **Oversized eager rules are dropped, not clipped.** A workspace with a rule
  over 6,000 characters previously received a truncated body and now receives
  none, with the omission recorded in the manifest notes.
- **Claude skills and agents with `disable-model-invocation` leave the
  catalog.** They were previously offered to the model despite the opt-out.

## Verification

597 tests pass. New coverage: `test_bounded_eager_reads.py` (10),
`test_invocation_policy.py` (9), `test_discovery_limits.py` (9),
`test_execute_root_budget.py` (4), plus additions to
`test_frontmatter_hardening.py`, `test_cursor_rule_activation.py` and
`test_scoped_instructions.py`.

The bounded-read tests assert against the read operation itself: they serve the
file through a handle that fails the test if an unbounded read is requested,
and assert the maximum size actually requested. A call-count test would not
detect a single full-file read.
