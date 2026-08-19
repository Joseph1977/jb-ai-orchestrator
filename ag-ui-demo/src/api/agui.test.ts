// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { closeThread, CloseThreadError } from './agui';
import * as client from './client';
import type { AGUIThreadCloseResponse } from '../types/agui';

describe('closeThread', () => {
  beforeEach(() => {
    vi.spyOn(client, 'postRaw');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns immediately on 200 closed', async () => {
    const body: AGUIThreadCloseResponse = {
      threadId: 't1',
      status: 'closed',
      alreadyClosed: false,
      discardedHolds: 0,
      runtimeDeleted: true,
      workspaceDeleted: true,
      workspaceDeletedCount: 1,
    };
    vi.mocked(client.postRaw).mockResolvedValueOnce({ status: 200, body });

    const result = await closeThread({ threadId: 't1', maxAttempts: 3, backoffMs: 1 });
    expect(result).toEqual(body);
    expect(client.postRaw).toHaveBeenCalledTimes(1);
    expect(client.postRaw).toHaveBeenCalledWith('/api/ag-ui/threads/t1/close', {});
  });

  it('retries 202 closing until 200 closed', async () => {
    const closing: AGUIThreadCloseResponse = {
      threadId: 't1',
      status: 'closing',
      alreadyClosed: false,
      discardedHolds: 1,
      runtimeDeleted: false,
      workspaceDeleted: false,
      workspaceDeletedCount: 0,
    };
    const closed: AGUIThreadCloseResponse = {
      ...closing,
      status: 'closed',
      runtimeDeleted: true,
      workspaceDeleted: true,
      workspaceDeletedCount: 1,
    };
    vi.mocked(client.postRaw)
      .mockResolvedValueOnce({ status: 202, body: closing })
      .mockResolvedValueOnce({ status: 200, body: closed });

    const result = await closeThread({ threadId: 't1', maxAttempts: 3, backoffMs: 1 });
    expect(result.status).toBe('closed');
    expect(client.postRaw).toHaveBeenCalledTimes(2);
  });

  it('fails clearly when 202 persists through max attempts', async () => {
    const closing: AGUIThreadCloseResponse = {
      threadId: 't1',
      status: 'closing',
      alreadyClosed: false,
      discardedHolds: 0,
      runtimeDeleted: false,
      workspaceDeleted: false,
      workspaceDeletedCount: 0,
    };
    vi.mocked(client.postRaw).mockResolvedValue({ status: 202, body: closing });

    await expect(
      closeThread({ threadId: 't1', maxAttempts: 2, backoffMs: 1 }),
    ).rejects.toMatchObject({
      name: 'CloseThreadError',
      httpStatus: 202,
      message: expect.stringContaining('still closing after 2 attempt'),
    });
    expect(client.postRaw).toHaveBeenCalledTimes(2);
  });

  it('fails clearly on unexpected status', async () => {
    vi.mocked(client.postRaw).mockResolvedValueOnce({
      status: 409,
      body: { detail: 'RUN_CONFLICT' },
    });

    await expect(
      closeThread({ threadId: 't1', maxAttempts: 1, backoffMs: 1 }),
    ).rejects.toBeInstanceOf(CloseThreadError);
  });

  it('clamps invalid maxAttempts so close still runs', async () => {
    const body: AGUIThreadCloseResponse = {
      threadId: 't1',
      status: 'closed',
      alreadyClosed: false,
      discardedHolds: 0,
      runtimeDeleted: true,
      workspaceDeleted: false,
      workspaceDeletedCount: 0,
    };
    vi.mocked(client.postRaw).mockResolvedValueOnce({ status: 200, body });

    await closeThread({ threadId: 't1', maxAttempts: Number.NaN, backoffMs: 1 });
    expect(client.postRaw).toHaveBeenCalledTimes(1);
  });
});
