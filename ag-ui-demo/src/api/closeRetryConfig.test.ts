// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  clampCloseBackoffMs,
  clampCloseMaxAttempts,
  parseCloseBackoffMs,
  parseCloseMaxAttempts,
  resolveCloseRetryConfig,
} from './closeRetryConfig';

describe('closeRetryConfig', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('clamps invalid maxAttempts to fallback', () => {
    expect(clampCloseMaxAttempts(Number.NaN)).toBe(5);
    expect(clampCloseMaxAttempts(-1)).toBe(5);
    expect(clampCloseMaxAttempts(0)).toBe(5);
    expect(clampCloseMaxAttempts(2.9)).toBe(2);
  });

  it('caps excessive maxAttempts at 20', () => {
    expect(clampCloseMaxAttempts(100)).toBe(20);
    expect(clampCloseMaxAttempts(20)).toBe(20);
    expect(clampCloseMaxAttempts(21)).toBe(20);
  });

  it('clamps invalid backoffMs to fallback', () => {
    expect(clampCloseBackoffMs(Number.NaN)).toBe(500);
    expect(clampCloseBackoffMs(-10)).toBe(500);
    expect(clampCloseBackoffMs(250.7)).toBe(250);
  });

  it('parses env values with invalid strings falling back', () => {
    vi.stubEnv('VITE_CLOSE_RETRY_MAX_ATTEMPTS', 'not-a-number');
    vi.stubEnv('VITE_CLOSE_RETRY_BACKOFF_MS', '-5');
    expect(parseCloseMaxAttempts(import.meta.env.VITE_CLOSE_RETRY_MAX_ATTEMPTS)).toBe(5);
    expect(parseCloseBackoffMs(import.meta.env.VITE_CLOSE_RETRY_BACKOFF_MS)).toBe(500);
  });

  it('resolveCloseRetryConfig uses clamped opts and env', () => {
    vi.stubEnv('VITE_CLOSE_RETRY_MAX_ATTEMPTS', '3');
    vi.stubEnv('VITE_CLOSE_RETRY_BACKOFF_MS', '1000');
    expect(resolveCloseRetryConfig()).toEqual({ maxAttempts: 3, backoffMs: 1000 });
    expect(resolveCloseRetryConfig({ maxAttempts: Number.NaN, backoffMs: -1 })).toEqual({
      maxAttempts: 5,
      backoffMs: 500,
    });
    vi.stubEnv('VITE_CLOSE_RETRY_MAX_ATTEMPTS', '999');
    expect(resolveCloseRetryConfig().maxAttempts).toBe(20);
  });
});
