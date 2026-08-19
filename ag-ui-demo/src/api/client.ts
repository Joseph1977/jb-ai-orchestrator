// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

const BASE_URL =
  import.meta.env.VITE_MAIN_AGENT_BASE_URL ?? 'http://localhost:8000';

let _apiKey: string | null = null;

export function setApiKey(key: string | null) {
  _apiKey = key;
}

function headers(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json', ...extra };
  if (_apiKey) h['Authorization'] = `Bearer ${_apiKey}`;
  return h;
}

export interface PostResult<T = unknown> {
  status: number;
  body: T;
}

export async function postRaw<T = unknown>(path: string, body: unknown): Promise<PostResult<T>> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let parsed: T;
  try {
    parsed = text ? (JSON.parse(text) as T) : ({} as T);
  } catch {
    parsed = text as unknown as T;
  }
  return { status: res.status, body: parsed };
}

export async function post<T = unknown>(path: string, body: unknown): Promise<T> {
  const { status, body: parsed } = await postRaw<T>(path, body);
  if (!status || status < 200 || status >= 300) {
    throw new Error(`POST ${path} → ${status}: ${JSON.stringify(parsed)}`);
  }
  return parsed;
}

export async function get<T = unknown>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, { headers: headers() });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`GET ${path} → ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

/**
 * POST that returns the raw Response (for SSE streaming).
 */
export async function postStream(path: string, body: unknown): Promise<Response> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: headers({ Accept: 'text/event-stream' }),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`POST (stream) ${path} → ${res.status}: ${text}`);
  }
  return res;
}

export function sseUrl(path: string): string {
  return `${BASE_URL}${path}`;
}

export { BASE_URL };
