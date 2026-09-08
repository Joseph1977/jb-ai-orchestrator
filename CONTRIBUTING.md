# Contributing

Thanks for considering a contribution. This is a personal open-source project,
so reviews happen when time allows — opening an issue before a large change
will save you effort.

## What belongs in the engine

The orchestrator is a generic execution engine. It has to run any workflow,
authored by any tool, against any provider. Every rule it enforces should
answer one question: **does its absence let one run damage the system or
interfere with another run?**

The engine owns bounded execution and cancellation, releasing a run claim only
after its worker has stopped, recovering crash-orphaned claims, workspace
containment, isolation between conversations and users, and returning each
durable object to a genuinely executable state after a failed segment.

The engine does not own how much a workflow asks a model to write, how an answer
should be phrased, or which workflow-specific tool a step ought to call. A
workflow states those in its own rules and instructions.

Two consequences are contracts, and a change that breaks either will be asked
to change:

- **No new global default may shape model behaviour.** Infrastructure limits can
  be enforced globally. A behavioural or operator limit must be off by default,
  with caller-owned limits arriving per request and falling back to segment
  config, as `maxToolCalls` does.
- **A failed segment never closes or permanently disables its session.**
  `ExecutionRun` records the failed attempt while the enclosing execution,
  pending interaction, or thread returns to the state it actually had. Only an
  explicit lifecycle action makes a session non-runnable.

## Getting set up

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

Run the suite:

```bash
python -m pytest
```

The tests need no database, no LiteLLM endpoint and no network. If a change
makes a test require any of those, that is a signal the seam is in the wrong
place.

To run the service itself, follow the [Quick start](readme.md#quick-start).
The launchers create the git-ignored environment files for you; only
`.env.example` files are tracked, so you never edit a tracked file to configure
a local run.

## Making a change

1. Branch from `on-going-dev`. Pull requests target `on-going-dev`, not
   `master`; `master` moves only through a pull request from `on-going-dev`.
2. Keep the change and its tests in one commit series that tells a clear story.
   A bug fix should include the test that would have caught it.
3. Update the docs that own the behaviour you changed: `readme.md` for the
   caller-facing API, setup or configuration, and
   [docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md) for harness or lifecycle
   behaviour. Routine refactors, tests and comment-only edits need no
   documentation change.
4. Add a record to `changes/` — one file per change, named
   `YYYY-MM-DD-<short-slug>.md`, so entries never conflict on merge. Write it
   after the work is done: it records what shipped, not what was planned. Note
   the date, the branch or pull request, what changed, why, and any migration or
   breaking note. An Alembic revision always gets a migration note.
5. Rebase or merge the latest `on-going-dev` into your branch and resolve
   conflicts locally before opening or updating a pull request.

## House style

- Match the surrounding code. This codebase favours explicit, readable
  functions over clever ones.
- Comments explain constraints and trade-offs the code cannot express. They do
  not narrate what the next line does.
- Never commit real credentials. Application settings live in
  `src/.env/{ENV}/.env`, and secrets in tracked examples are placeholders of the
  form `__VARIABLE_NAME__`, substituted at deploy time. Files holding real
  values are git-ignored and must stay that way.
- Text is stored with LF endings, enforced by `.gitattributes`. `.bat`, `.cmd`
  and `.ps1` are checked out with CRLF because the Windows shell parses them.
- Do not add tool, model, vendor or assistant attribution anywhere — source
  comments, docs, commit messages and trailers, branch names, or pull request
  text. If your tooling inserts it automatically, remove it before pushing.

`git blame` should skip the line-ending normalization commit. Once per clone:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

## Migrations

Read [docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md) before adding an Alembic
revision. Persistence carries lifecycle guarantees, and a revision that loses
the executable state of an in-flight object is a correctness bug rather than a
schema detail.

## Reporting bugs and vulnerabilities

Functional bugs belong in the issue tracker. Security issues do not — see
[SECURITY.md](SECURITY.md) for private reporting.

## Licence

Contributions are accepted under the [Apache License, Version 2.0](LICENSE), the
licence covering this repository. You confirm you have the right to submit the
work under that licence.
