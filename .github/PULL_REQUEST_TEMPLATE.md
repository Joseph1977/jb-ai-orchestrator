# What changed

<!-- What this does, and why. Link the issue if there is one. -->

# Why it belongs in the engine

<!--
Only needed for behavioural changes. The engine exists to stop one run from
damaging the system or interfering with another run; workflow-specific policy
belongs in a workflow's own rules. See CONTRIBUTING.md.
-->

# Checklist

- [ ] Targets `on-going-dev`, and the latest `on-going-dev` is merged in with
      conflicts resolved locally
- [ ] `python -m pytest` passes, and the suite still needs no database, no
      LiteLLM endpoint and no network
- [ ] New behaviour has a test; a bug fix has the test that would have caught it
- [ ] Any new behavioural or operator setting defaults to off
- [ ] No failure path closes or permanently disables a session that was
      otherwise runnable
- [ ] Docs updated where the behaviour lives — `readme.md` for the caller-facing
      API, setup or configuration; `docs/ORCHESTRATOR.md` for harness or
      lifecycle behaviour
- [ ] A record added under `changes/` as `YYYY-MM-DD-<short-slug>.md`, with a
      migration note if there is an Alembic revision
- [ ] No credentials, private hostnames, or tool/vendor attribution anywhere in
      the diff or the commit messages

# Migration or breaking notes

<!-- Anything an existing deployment must do. Write "None" if there is nothing. -->
