# AGENTS.md

Instructions for anyone working in this repository, human or agent.

## What this service is

The orchestrator is a generic execution engine. It must run any workflow,
authored by any tool, against any provider. Everything it enforces must be
justified by one question: **does its absence let one run damage the system or
interfere with another run?**

**The engine owns:** bounded execution and cancellation; releasing a run claim
only after its worker has actually stopped; recovering crash-orphaned claims;
workspace containment; isolation between conversations and users; and
returning each durable object to its real executable state after a failed
segment.

**The engine does not own:** how much a workflow asks a model to write, how an
answer should be phrased, which workflow-specific tool a step ought to call, or
other product policy. A workflow that wants those behaviours states them in its
own rules and instructions.

Two consequences are contracts:

- No new global default may shape model behaviour. Infrastructure limits may
  be enforced globally. A behavioural or operator limit must be off by default;
  caller-owned limits arrive per request with a segment-config fallback, as
  `maxToolCalls` does.
- A failed segment never closes or permanently disables its session.
  `ExecutionRun` records the failed attempt, while the enclosing execution,
  pending interaction, or thread returns to the executable state it actually
  had. Closed and closing sessions remain non-runnable by explicit lifecycle
  action.

## Where you are working

| You are editing… | Also read… |
|---|---|
| `src/app/services/harness/**` | `docs/ORCHESTRATOR.md` §3 — adapter selection, eager caps, progressive disclosure |
| `src/app/services/workspace_io.py` | `docs/ORCHESTRATOR.md` §3 — workspace containment and discovery traversal |
| `src/app/services/context_compaction.py` | `docs/ORCHESTRATOR.md` §8 — offload, summarization, compaction, the `preCompact` hook |
| Lifecycle, resume, close, persistence | `docs/ORCHESTRATOR.md` §10, §12, §13 |
| `src/app/controllers/**`, public API | `readme.md` — the caller-facing contract |
| `alembic/versions/**` | `docs/ORCHESTRATOR.md` §13 before adding a revision |
| `ag-ui-demo/**` | `ag-ui-demo/README.md` |

`docs/ORCHESTRATOR.md` is the source of truth for harness and lifecycle
behaviour; `readme.md` is the source of truth for the caller-facing API.

## Contributing

- Branch from `origin/on-going-dev`, and open a pull request against
  `on-going-dev`. Do not commit directly to `on-going-dev` or `master`.
- Pull the latest `on-going-dev` into your branch and resolve conflicts locally
  before opening or updating a pull request.
- Update `master` only through a pull request from `on-going-dev`.
- Never push directly to `master` or `on-going-dev`.
- Do not merge your own pull request.
- An urgent fix to what is released branches from `origin/master` as
  `hotfix/<something>` and reaches `master` by pull request. Immediately after
  it merges, apply the same fix on a branch cut from the current
  `origin/on-going-dev` and open a second pull request there. Until that lands,
  `on-going-dev` lacks the fix and the next promotion would undo it. See
  `CONTRIBUTING.md` §Fixing production.
- Never commit real credentials. Application settings live in
  `src/.env/{ENV}/.env` and secrets there are placeholders of the form
  `__VARIABLE_NAME__`, substituted at deploy time. `.env` files holding real
  values are git-ignored and must stay that way.

## Keep the docs current

Update `readme.md` when the caller-facing API, setup/run steps, or
configuration changes, and `docs/ORCHESTRATOR.md` when harness or lifecycle
behaviour changes. Routine refactors, tests, and comment-only edits need no
documentation change.

Record completed work in `changes/` — one markdown file per change, named
`YYYY-MM-DD-<short-slug>.md`, so entries never conflict on merge. Write the
entry after the work is done: it records what shipped, not what is planned.
Each entry captures the date, the branch or pull request, what changed, why,
and any migration or breaking note. Alembic revisions always get a migration
note.

## Attribution

Do not add tool, model, vendor, or assistant attribution to any output. This
covers source comments, docs, generated files, commit messages and trailers,
branch names, and pull request titles, descriptions, and comments. If tooling
inserts such text automatically, remove it before pushing.
