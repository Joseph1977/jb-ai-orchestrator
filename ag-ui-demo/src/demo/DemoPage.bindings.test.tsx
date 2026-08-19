// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DemoPage from './DemoPage';
import { BINDING_OUTPUT_WITHOUT_INPUT_MESSAGE } from '../config/bindings';

const startRun = vi.hoisted(() => vi.fn());

vi.mock('../api/agui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/agui')>();
  return {
    ...actual,
    startRun: (...args: unknown[]) => startRun(...args),
  };
});

describe('DemoPage binding configuration', () => {
  beforeEach(() => {
    startRun.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    startRun.mockClear();
  });

  it('does not start a run when output is configured without input', async () => {
    vi.stubEnv('VITE_BINDING_OUTPUT_URI', '/workspaces/demo-output');
    const user = userEvent.setup();
    render(<DemoPage />);

    await user.click(screen.getByRole('button', { name: /^2 — Start Run$/ }));

    expect(startRun).not.toHaveBeenCalled();
    expect(
      screen.getByText((content) => content.includes(BINDING_OUTPUT_WITHOUT_INPUT_MESSAGE)),
    ).toBeInTheDocument();
  });
});
