# Refresh demo dependencies after the public-release audit

**Date:** 2026-09-08
**Branch / PR:** `fix/demo-dependency-vulnerabilities` → `on-going-dev`

## What changed

The `ag-ui-demo` lockfile now resolves patched compatible versions of Vite and
its transitive build and test dependencies.

## Why

The final public-release audit found six known vulnerabilities in the locked
demo toolchain, including five rated high severity. Updating the lockfile
removes those findings without changing the demo's declared dependency ranges.

## Migration or breaking notes

None. The update remains within the versions allowed by `package.json`.
