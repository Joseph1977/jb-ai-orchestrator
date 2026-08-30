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

## Third review round

### Discovery is bounded in work, not only in output (high)

The ceiling capped what was kept while leaving what was *done* unbounded. The
pruning walker buffered every match in the tree before yielding, candidates were
read before the ceiling was consulted, and both loops ran to completion after a
kind was full. Producing 200 entries from a large monorepo therefore cost one
metadata read per candidate and memory proportional to the whole tree.

Traversal is now depth-first over each directory's name-sorted entries,
descending as each directory is met. That is lexicographic order over path
components — exactly what `sorted()` produces — so ordering is unchanged while
memory drops to the frontier along the current path, O(depth × directory width).
The reviewer's observation that streaming and sorted order are not in tension
was correct; there was no trade-off to make.

The walk uses an explicit stack rather than recursion. The filesystem accepts
trees far deeper than the interpreter allows — 2,045 levels against a recursion
limit of 1,000 on Linux — so a recursive walker would abort discovery on one.

Candidates are classified `ADDED`, `DUPLICATE` or `CEILING`. A boolean conflated
"already catalogued" with "no room left", so a duplicate met while a bucket was
full would have stopped traversal as though the workspace had overflowed. Checks
run duplicate first, then ceiling, then read: a capped kind costs no reads, and a
workspace holding exactly 200 of a kind reports no omission. Claude's
`*/SKILL.md` glob also skips pruned directory names, which `*` matched directly
under `skills/`.

Two invariants are explicit in the walker. An unreadable directory or entry is
logged and skipped, so one permission-denied subtree cannot abort discovery. And
symlinks cannot lead discovery out of the workspace: linked directories are never
descended, and a linked file is read only when its target resolves inside the
workspace.

### `initiate` reports the root-budget failure with its code (medium)

Execute gained `ROOT_INSTRUCTIONS_TOO_LARGE` in the previous round, but
`initiate` reaches the same condition through `collect_manifest` and fell to its
generic handler, returning HTTP 400 with no code. Callers saw one condition
reported two ways depending on which call hit it first. It now returns the same
code.

### Endpoint lifecycle is covered (low)

The previous round tested the prompt builder in isolation, leaving the endpoint
behaviour around it unverified. `test_endpoint_root_budget.py` calls both
endpoint coroutines with their module-level collaborators mocked and pins the
status code and error code, that the execution is finalized as failed, that the
run is closed rather than left active, and that the workspace is cleaned up —
plus the success paths and the generic-handler path, so the new branch cannot
shadow it.

## Fourth review round

### Containment moved out of the walker and into one opener (high)

The previous round put symlink checks in the directory walker, which meant the
guarantee covered only the paths that walked. It missed a symlinked discovery
root — `.claude/rules -> /outside` was traversed, because only *children* were
checked, never the root itself — and it did not apply at all to adapter
enumeration or root-instruction reads, which open files without walking
anything. The file check was also resolve-then-open, so a link swapped in
between the two calls would be followed.

Containment is now a property of opening a file, not of how the path was found.
A path is held as validated components relative to the workspace, and absolute
paths, `..`, empty components and embedded separators are rejected before any
syscall. The opener then walks those components one at a time relative to a
descriptor anchored on the workspace, opening each with `O_NOFOLLOW`. The open
is the check, so there is no window to race, and because `O_NOFOLLOW` constrains
only the final component of the path it is given, feeding components one at a
time is what extends the guarantee to symlinked parents rather than just
symlinked leaves.

Three consequences are deliberate. Symlinks are refused rather than resolved,
including ones pointing inside the workspace, because resolving is the raceable
pattern being removed. Leaves must be regular files, verified with `fstat` and
opened with `O_NONBLOCK` so a planted FIFO cannot block discovery before it is
rejected. And where the platform lacks `os.open(dir_fd=)` or `os.scandir(fd)`
the reader refuses to construct, rather than falling back to path-based opens
that would silently drop the guarantee.

The opener re-anchors per operation instead of holding a descriptor per level,
so it uses a constant number of descriptors and correctness does not depend on
the process descriptor limit. The cost is O(depth) additional opens.

Every remaining `Path.glob` and `rglob` in the adapters was replaced, including
the non-recursive ones, so no alternate enumeration path survives to bypass the
opener.

### One candidate transaction owns the whole sequence (high)

Deduplication, the ceiling check, the metadata read and the append were
scattered across three adapters and two shared discovery functions, each
responsible for getting the order right. They now go through a single
`consider()` operation which sets and validates the entry's `path` and `kind`
itself; adapters supply only provider-specific name, description, policy, scope
and source. The ordering guarantees are structural rather than a convention each
call site has to remember, and an adapter can no longer file an entry under a
path it never opened.

`REJECTED` joins the outcome enum. A file that cannot be read safely is skipped
and logged rather than catalogued from its filename — an entry the model cannot
read is worse than no entry, because it will try. A readable file with no
frontmatter still keeps its filename/prose fallback.

### Confirmed-capped kinds no longer walk (medium)

`_discover_primitives` called `iter_skill_files()` even with `"skill"` already
capped, and `_discover_scoped_instructions` walked the whole workspace with
`"rule"` capped. Both now short-circuit before traversing, per-pattern roots are
skipped for capped kinds, and discovery stops entirely once every kind has
overflowed. Detecting the one excess candidate still happens without reading it.

### `AddOutcome` is compared by identity (medium)

Cursor and Claude wrote `if added and policy is LoadingPolicy.EAGER`. Every
string enum member is truthy, including `CEILING`, so a rule refused by the
ceiling still had its body read and injected into eager context — the ceiling
bounded the catalog while the eager path ignored it. All comparisons are now
`is AddOutcome.ADDED`, and eager content is read only after a candidate is
actually added.

### Read failures are no longer reported as size failures (medium)

`read_root_instructions` raised one exception for both an over-budget file and
an unreadable one, and both mapped to `ROOT_INSTRUCTIONS_TOO_LARGE`. An operator
hitting a permissions error was told to split their playbook. The exception now
splits into `RootInstructionTooLarge` and `RootInstructionUnreadable`, each
carrying its own `code`, and the controllers report `exc.code` rather than a
fixed constant.

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
- **`initiate` can now return `ROOT_INSTRUCTIONS_TOO_LARGE`** (HTTP 400) where
  it previously returned 400 with no code. Callers matching on the message
  rather than the code are unaffected; the message is unchanged.
- **Symlinks are no longer followed** during discovery — not directories, not
  files, and not a symlinked discovery root. A workspace that reached primitives
  through a link will no longer see them. This closes a path out of the sandbox
  and was never intended behaviour. Note this is stricter than the third round,
  which still read a linked file whose target resolved inside the workspace.
- **Non-regular files are never read.** A FIFO or device node named `AGENTS.md`
  is rejected instead of opened.
- **New error code `ROOT_INSTRUCTIONS_UNREADABLE`** (HTTP 400). Root instruction
  failures that are not about size — permissions, I/O, or a path the opener
  refuses — previously reported `ROOT_INSTRUCTIONS_TOO_LARGE`. A caller matching
  on that code for read failures must now match both.

## Verification

650 tests pass on Python 3.11 (the CI version) and on 3.14. New coverage:
`test_workspace_containment.py` (24) and a rewritten
`test_discovery_traversal.py` (18), plus additions to
`test_bounded_eager_reads.py` and `test_endpoint_root_budget.py`.

The bounded-read tests assert against the read operation itself: they wrap
`os.fdopen` so the real descriptor-anchored traversal, `O_NOFOLLOW` and `fstat`
checks all still run underneath, fail the test if an unbounded read is
requested, and assert the maximum size actually requested. A call-count test
would not detect a single full-file read.

Containment is tested per entry point rather than through one traversal, since
the finding was precisely that one traversal did not cover the others:
symlinked root, symlinked parent component, symlinked leaf, an inside-the-
workspace link, a broken link, a FIFO, and a directory opened as a file. The
swap-after-traversal case uses a deterministic hook between component traversal
and leaf open rather than a thread race, because the claim under test is that
the guarantee holds whenever the swap lands, not that one interleaving happens
to be caught.

Descriptor behaviour is asserted by counting the process's open descriptors
across repeated success and failure operations, and across a 300-level tree, so
a leak or per-level accumulation fails the suite. Per the review, no timing
assertions were added.

The capped-kind and ceiling guarantees are asserted as absence of work — zero
`os.scandir` calls for a confirmed-capped kind, zero opens for the 201st
candidate — rather than as absence of output.
