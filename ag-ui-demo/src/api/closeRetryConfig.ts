// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

export const CLOSE_RETRY_FALLBACK_MAX_ATTEMPTS = 5;
export const CLOSE_RETRY_FALLBACK_BACKOFF_MS = 500;
export const CLOSE_RETRY_MAX_ATTEMPTS_CAP = 20;

export function clampCloseMaxAttempts(value: number): number {
  if (!Number.isFinite(value) || value < 1) {
    return CLOSE_RETRY_FALLBACK_MAX_ATTEMPTS;
  }
  return Math.min(CLOSE_RETRY_MAX_ATTEMPTS_CAP, Math.floor(value));
}

export function clampCloseBackoffMs(value: number): number {
  if (!Number.isFinite(value) || value < 0) {
    return CLOSE_RETRY_FALLBACK_BACKOFF_MS;
  }
  return Math.floor(value);
}

export function parseCloseMaxAttempts(raw: string | undefined): number {
  const parsed = Number.parseInt(raw ?? '', 10);
  return clampCloseMaxAttempts(parsed);
}

export function parseCloseBackoffMs(raw: string | undefined): number {
  const parsed = Number.parseInt(raw ?? '', 10);
  return clampCloseBackoffMs(parsed);
}

export interface CloseRetryResolved {
  maxAttempts: number;
  backoffMs: number;
}

export function resolveCloseRetryConfig(opts?: {
  maxAttempts?: number;
  backoffMs?: number;
}): CloseRetryResolved {
  const maxAttempts =
    opts?.maxAttempts !== undefined
      ? clampCloseMaxAttempts(opts.maxAttempts)
      : parseCloseMaxAttempts(import.meta.env.VITE_CLOSE_RETRY_MAX_ATTEMPTS);

  const backoffMs =
    opts?.backoffMs !== undefined
      ? clampCloseBackoffMs(opts.backoffMs)
      : parseCloseBackoffMs(import.meta.env.VITE_CLOSE_RETRY_BACKOFF_MS);

  return { maxAttempts, backoffMs };
}
