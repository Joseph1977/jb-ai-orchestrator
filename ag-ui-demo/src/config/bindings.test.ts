// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  buildConfiguredRunBindings,
  BindingConfigurationError,
  BINDING_OUTPUT_WITHOUT_INPUT_MESSAGE,
} from './bindings';

describe('buildConfiguredRunBindings', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('returns null when no binding env is configured', () => {
    vi.stubEnv('VITE_BINDING_INPUT_URI', '');
    vi.stubEnv('VITE_BINDING_OUTPUT_URI', '');
    expect(buildConfiguredRunBindings()).toBeNull();
  });

  it('builds workflow input with in_place_read_only and no credentials', () => {
    vi.stubEnv('VITE_BINDING_INPUT_URI', '/workspaces/demo-input');
    vi.stubEnv('VITE_BINDING_INPUT_RELATIVE_PATH', 'playbook');

    const bindings = buildConfiguredRunBindings();
    expect(bindings).toEqual({
      input: {
        type: 'shared_folder',
        uri: '/workspaces/demo-input',
        relativePath: 'playbook',
        materialization: 'in_place_read_only',
      },
      mode: 'workflow',
    });
    expect(bindings?.output).toBeUndefined();
  });

  it('throws when output is configured without input', () => {
    vi.stubEnv('VITE_BINDING_OUTPUT_URI', '/workspaces/demo-output');

    expect(() => buildConfiguredRunBindings()).toThrow(BindingConfigurationError);
    expect(() => buildConfiguredRunBindings()).toThrow(BINDING_OUTPUT_WITHOUT_INPUT_MESSAGE);
  });

  it('includes local shared_folder output without credentials when input is configured', () => {
    vi.stubEnv('VITE_BINDING_INPUT_URI', '/workspaces/demo-input');
    vi.stubEnv('VITE_BINDING_OUTPUT_URI', '/workspaces/demo-output');
    vi.stubEnv('VITE_BINDING_OUTPUT_RELATIVE_PATH', 'artifacts');

    const bindings = buildConfiguredRunBindings();
    expect(bindings).toEqual({
      input: {
        type: 'shared_folder',
        uri: '/workspaces/demo-input',
        relativePath: '.',
        materialization: 'in_place_read_only',
      },
      output: {
        type: 'shared_folder',
        uri: '/workspaces/demo-output',
        relativePath: 'artifacts',
      },
      mode: 'workflow',
    });
    expect(JSON.stringify(bindings)).not.toMatch(/credential|token|secret/i);
  });
});
