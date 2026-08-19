// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { postStream, post, postRaw } from './client';
import {
  resolveCloseRetryConfig,
  type CloseRetryResolved,
} from './closeRetryConfig';
import type {
  AGUIRunRequest,
  AGUIEvent,
  FrontendTool,
  AGUIRunMessage,
  ExecuteRequestBody,
  ExecuteRequestResponse,
  ResumeRunBody,
  AGUIThreadCloseResponse,
  RunBindings,
} from '../types/agui';
import { EventType } from '../types/agui';

export const DEFAULT_THREAD_ID =
  import.meta.env.VITE_THREAD_ID ?? 'ag-ui-demo-thread';

export { resolveCloseRetryConfig, type CloseRetryResolved };
export {
  clampCloseMaxAttempts,
  clampCloseBackoffMs,
  parseCloseMaxAttempts,
  parseCloseBackoffMs,
} from './closeRetryConfig';

export class CloseThreadError extends Error {
  readonly httpStatus?: number;

  constructor(message: string, httpStatus?: number) {
    super(message);
    this.name = 'CloseThreadError';
    this.httpStatus = httpStatus;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface CloseThreadOptions {
  threadId?: string;
  maxAttempts?: number;
  backoffMs?: number;
}

// ── helpers ──────────────────────────────────────────────

function uuid(): string {
  return crypto.randomUUID();
}

// ── 1) Bind-only registration ────────────────────────────

export async function registerTools(
  tools: FrontendTool[],
  threadId: string = DEFAULT_THREAD_ID,
): Promise<AGUIEvent[]> {
  const body: AGUIRunRequest = {
    threadId,
    runId: uuid(),
    messages: [],
    frontendTools: tools,
    context: [],
  };
  return consumeSSE(body);
}

// ── 2) Start an interactive SSE run ──────────────────────

export interface RunCallbacks {
  onEvent: (event: AGUIEvent) => void;
  onError?: (err: Error) => void;
  onDone?: () => void;
}

export async function startRun(
  messages: AGUIRunMessage[],
  tools: FrontendTool[],
  callbacks: RunCallbacks,
  opts?: {
    threadId?: string;
    runId?: string;
    model?: string;
    maxToolCalls?: number;
    bindings?: RunBindings | null;
  },
): Promise<void> {
  const body: AGUIRunRequest = {
    threadId: opts?.threadId ?? DEFAULT_THREAD_ID,
    runId: opts?.runId ?? uuid(),
    messages,
    frontendTools: tools,
    context: [],
    model: opts?.model,
    maxToolCalls: opts?.maxToolCalls,
  };

  const bindings = opts?.bindings;
  if (bindings) {
    body.input = bindings.input;
    if (bindings.output) body.output = bindings.output;
    if (bindings.mode) body.mode = bindings.mode;
  }

  try {
    const res = await postStream('/api/ag-ui/run', body);
    await readSSEStream(res, callbacks);
  } catch (err) {
    callbacks.onError?.(err instanceof Error ? err : new Error(String(err)));
  }
}

// ── 3) Submit tool response (resume paused run) ─────────

export async function submitToolResponse(
  toolCallId: string,
  result: unknown,
  threadId: string = DEFAULT_THREAD_ID,
  runId?: string,
): Promise<AGUIEvent[]> {
  const body: AGUIRunRequest = {
    threadId,
    runId: runId ?? uuid(),
    messages: [],
    frontendTools: [],
    context: [],
    state: { toolCallId, result },
  };
  return consumeSSE(body);
}

// ── 4) API-driven execute + resume ───────────────────────

export async function executeRequest(
  body: ExecuteRequestBody,
): Promise<ExecuteRequestResponse> {
  return post<ExecuteRequestResponse>('/v1/agent/executeRequest', body);
}

export async function resumeRun(
  body: ResumeRunBody,
): Promise<ExecuteRequestResponse> {
  return post<ExecuteRequestResponse>('/v1/agent/resumeRun', body);
}

// ── 5) Close AG-UI thread lifecycle ──────────────────────

export async function closeThread(
  opts?: CloseThreadOptions,
): Promise<AGUIThreadCloseResponse> {
  const threadId = opts?.threadId ?? DEFAULT_THREAD_ID;
  const { maxAttempts, backoffMs } = resolveCloseRetryConfig(opts);
  const path = `/api/ag-ui/threads/${encodeURIComponent(threadId)}/close`;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const { status, body } = await postRaw<AGUIThreadCloseResponse>(path, {});

    if (status === 200 && body.status === 'closed') {
      return body;
    }

    if (status === 202 && body.status === 'closing') {
      if (attempt >= maxAttempts) {
        throw new CloseThreadError(
          `Thread ${threadId} still closing after ${maxAttempts} attempt(s)`,
          202,
        );
      }
      await sleep(backoffMs);
      continue;
    }

    throw new CloseThreadError(
      `POST ${path} → ${status}: ${JSON.stringify(body)}`,
      status,
    );
  }

  throw new CloseThreadError(
    `Thread ${threadId} close exhausted without reaching a terminal response`,
  );
}

// ── internal: consume SSE into an array ──────────────────

async function consumeSSE(body: AGUIRunRequest): Promise<AGUIEvent[]> {
  const events: AGUIEvent[] = [];
  const res = await postStream('/api/ag-ui/run', body);
  await readSSEStream(res, {
    onEvent: (e) => events.push(e),
  });
  return events;
}

// ── internal: read SSE from a Response ───────────────────

async function readSSEStream(
  res: Response,
  callbacks: RunCallbacks,
): Promise<void> {
  const reader = res.body?.getReader();
  if (!reader) {
    callbacks.onError?.(new Error('No readable stream in response'));
    return;
  }

  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith(':')) continue;

        if (trimmed.startsWith('data:')) {
          const jsonStr = trimmed.slice(5).trim();
          if (!jsonStr || jsonStr === '[DONE]') continue;
          try {
            const event = JSON.parse(jsonStr) as AGUIEvent;
            callbacks.onEvent(event);

            if (
              event.type === EventType.RUN_FINISHED ||
              event.type === EventType.RUN_ERROR
            ) {
              callbacks.onDone?.();
            }
          } catch {
            // skip malformed JSON lines
          }
        }
      }
    }
    callbacks.onDone?.();
  } catch (err) {
    callbacks.onError?.(err instanceof Error ? err : new Error(String(err)));
  } finally {
    reader.releaseLock();
  }
}
