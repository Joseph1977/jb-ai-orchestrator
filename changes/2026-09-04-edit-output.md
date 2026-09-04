# Durable output supports targeted edits

**Date:** 2026-09-04
**Branch:** `feature/edit-output`
**Target:** `on-going-dev`

## What changed

Added `edit_output_local`, an exact string-replacement tool for existing files
in a bound durable output store. It mirrors `edit_file_local` semantics,
including rejection of empty, missing, and ambiguous matches unless
`replace_all=true`. The tool works through both shared-folder and Azure Blob
backends, respects existing read and write limits, remains hidden without an
output binding, and redacts replacement text from logs.

The output-binding prompt and public documentation now distinguish full writes
from targeted edits and document the read-modify-write concurrency behavior.
The existing write, read, and list output tools now also describe their paths
as relative to the bound output root rather than to the input workspace.

## Why

Previously every durable state update required the model to reconstruct and
replace the complete file. Targeted replacement reduces accidental loss of
unrelated state while leaving workflow content and update policy with the
workflow.

## Migration and compatibility

No migration or caller change is required. Existing output tools retain their
behavior. Concurrent targeted edits are last-writer-wins; Azure Blob writes do
not use ETag preconditions.

## Verification

- Focused output, prompt, and fresh-run tool tests: 45 passed.
- Full orchestrator suite: 763 passed.
