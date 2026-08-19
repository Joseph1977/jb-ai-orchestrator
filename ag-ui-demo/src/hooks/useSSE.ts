// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useCallback, useState } from 'react';
import { sseUrl } from '../api/client';
import type { AGUIEvent } from '../types/agui';

/**
 * Hook that opens an SSE connection to GET /api/ag-ui/events
 * (the global fan-out stream) and feeds parsed events to a callback.
 */
export function useGlobalSSE(
  onEvent: (event: AGUIEvent) => void,
  enabled = true,
) {
  const cbRef = useRef(onEvent);
  cbRef.current = onEvent;

  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) return;

    const url = sseUrl('/api/ag-ui/events');
    const es = new EventSource(url);

    es.onopen = () => setConnected(true);

    es.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as AGUIEvent;
        cbRef.current(event);
      } catch {
        // skip unparseable messages
      }
    };

    es.onerror = () => {
      setConnected(false);
    };

    return () => {
      es.close();
      setConnected(false);
    };
  }, [enabled]);

  return { connected };
}
