// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import type { ExecutionMode, LocationBinding, RunBindings } from '../types/agui';

function trimOrUndefined(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

export class BindingConfigurationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BindingConfigurationError';
  }
}

/** Demo/web never send output without workflow input. */
export const BINDING_OUTPUT_WITHOUT_INPUT_MESSAGE =
  'VITE_BINDING_OUTPUT_URI requires VITE_BINDING_INPUT_URI (demo never sends output without workflow input)';

function sharedFolderOutput(uri: string): LocationBinding {
  return {
    type: 'shared_folder',
    uri,
    relativePath: trimOrUndefined(import.meta.env.VITE_BINDING_OUTPUT_RELATIVE_PATH) ?? '.',
  };
}

/**
 * Build optional AG-UI run bindings from Vite env.
 * Returns null when no input URI is configured (plain chat — no bindings).
 * Throws when output is configured without input.
 */
export function buildConfiguredRunBindings(): RunBindings | null {
  const inputUri = trimOrUndefined(import.meta.env.VITE_BINDING_INPUT_URI);
  const outputUri = trimOrUndefined(import.meta.env.VITE_BINDING_OUTPUT_URI);

  if (!inputUri) {
    if (outputUri) {
      throw new BindingConfigurationError(BINDING_OUTPUT_WITHOUT_INPUT_MESSAGE);
    }
    return null;
  }

  const bindings: RunBindings = {
    input: {
      type: 'shared_folder',
      uri: inputUri,
      relativePath: trimOrUndefined(import.meta.env.VITE_BINDING_INPUT_RELATIVE_PATH) ?? '.',
      materialization: 'in_place_read_only',
    },
    mode: 'workflow' satisfies ExecutionMode,
  };

  if (outputUri) {
    bindings.output = sharedFolderOutput(outputUri);
  }

  return bindings;
}
