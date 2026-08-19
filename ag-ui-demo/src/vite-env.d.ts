/// <reference types="vite/client" />
// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

interface ImportMetaEnv {
  readonly VITE_MAIN_AGENT_BASE_URL?: string;
  readonly VITE_THREAD_ID?: string;
  readonly VITE_BINDING_INPUT_URI?: string;
  readonly VITE_BINDING_INPUT_RELATIVE_PATH?: string;
  readonly VITE_BINDING_OUTPUT_URI?: string;
  readonly VITE_BINDING_OUTPUT_RELATIVE_PATH?: string;
  readonly VITE_CLOSE_RETRY_MAX_ATTEMPTS?: string;
  readonly VITE_CLOSE_RETRY_BACKOFF_MS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
