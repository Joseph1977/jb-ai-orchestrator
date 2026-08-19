// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

/* ──────────────────────────────────────────────────────────
 *  AG-UI shared types — mirrors the protocol event shapes
 *  used by the jb-ai-orchestrator AG-UI controller.
 * ────────────────────────────────────────────────────────── */

// ── Event type enum ──────────────────────────────────────

export enum EventType {
  RUN_STARTED = 'RUN_STARTED',
  RUN_FINISHED = 'RUN_FINISHED',
  RUN_ERROR = 'RUN_ERROR',
  STEP_STARTED = 'STEP_STARTED',
  STEP_FINISHED = 'STEP_FINISHED',
  TEXT_MESSAGE_START = 'TEXT_MESSAGE_START',
  TEXT_MESSAGE_CONTENT = 'TEXT_MESSAGE_CONTENT',
  TEXT_MESSAGE_END = 'TEXT_MESSAGE_END',
  TOOL_CALL_START = 'TOOL_CALL_START',
  TOOL_CALL_ARGS = 'TOOL_CALL_ARGS',
  TOOL_CALL_END = 'TOOL_CALL_END',
  TOOL_CALL_RESULT = 'TOOL_CALL_RESULT',
  STATE_SNAPSHOT = 'STATE_SNAPSHOT',
  STATE_DELTA = 'STATE_DELTA',
}

// ── Base event ───────────────────────────────────────────

export interface BaseEvent {
  type: EventType;
  timestamp?: string;
}

// ── Lifecycle events ─────────────────────────────────────

export interface RunStartedEvent extends BaseEvent {
  type: EventType.RUN_STARTED;
  threadId: string;
  runId: string;
}

export interface RunFinishedEvent extends BaseEvent {
  type: EventType.RUN_FINISHED;
  threadId: string;
  runId: string;
  result?: unknown;
}

export interface RunErrorEvent extends BaseEvent {
  type: EventType.RUN_ERROR;
  message: string;
  code?: string;
}

// ── Text message events ──────────────────────────────────

export interface TextMessageStartEvent extends BaseEvent {
  type: EventType.TEXT_MESSAGE_START;
  messageId: string;
  role: string;
}

export interface TextMessageContentEvent extends BaseEvent {
  type: EventType.TEXT_MESSAGE_CONTENT;
  messageId: string;
  delta: string;
}

export interface TextMessageEndEvent extends BaseEvent {
  type: EventType.TEXT_MESSAGE_END;
  messageId: string;
}

// ── Tool call events ─────────────────────────────────────

export interface ToolCallStartEvent extends BaseEvent {
  type: EventType.TOOL_CALL_START;
  toolCallId: string;
  toolCallName: string;
  parentMessageId?: string;
  awaitsResponse?: boolean;
}

export interface ToolCallArgsEvent extends BaseEvent {
  type: EventType.TOOL_CALL_ARGS;
  toolCallId: string;
  delta: string;
}

export interface ToolCallEndEvent extends BaseEvent {
  type: EventType.TOOL_CALL_END;
  toolCallId: string;
}

// ── Union of all handled events ──────────────────────────

export type AGUIEvent =
  | RunStartedEvent
  | RunFinishedEvent
  | RunErrorEvent
  | TextMessageStartEvent
  | TextMessageContentEvent
  | TextMessageEndEvent
  | ToolCallStartEvent
  | ToolCallArgsEvent
  | ToolCallEndEvent;

// ── Tool definition (frontend → server) ──────────────────

export interface ToolParameterProperty {
  type: string;
  description?: string;
  enum?: string[];
  items?: { type: string };
}

export interface ToolParameters {
  type: 'object';
  properties: Record<string, ToolParameterProperty>;
  required?: string[];
}

export interface FrontendTool {
  name: string;
  description: string;
  parameters: ToolParameters;
  extensions?: {
    awaitsResponse?: boolean;
  };
}

// ── AG-UI run request body ───────────────────────────────

export interface AGUIRunMessage {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  toolCallId?: string;
}

export type LocationBindingType = 'shared_folder';

export type Materialization = 'copy' | 'in_place_read_only';

export type ExecutionMode = 'workflow' | 'working_copy';

export interface LocationBinding {
  type: LocationBindingType;
  uri: string;
  relativePath?: string;
  materialization?: Materialization;
}

/** Fresh-run input/output/mode; resume omits these (server persists bindings). */
export interface RunBindings {
  input: LocationBinding;
  output?: LocationBinding;
  mode?: ExecutionMode;
}

export interface AGUIRunRequest {
  threadId: string;
  runId?: string;
  messages?: AGUIRunMessage[];
  frontendTools?: FrontendTool[];
  context?: unknown[];
  state?: {
    toolCallId: string;
    result?: unknown;
    error?: string;
  };
  model?: string;
  maxToolCalls?: number;
  input?: LocationBinding | null;
  output?: LocationBinding;
  mode?: ExecutionMode;
}

export interface AGUIThreadCloseResponse {
  threadId: string;
  status: 'closed' | 'closing';
  alreadyClosed: boolean;
  discardedHolds: number;
  runtimeDeleted: boolean;
  workspaceDeleted: boolean;
  workspaceDeletedCount: number;
}

// ── executeRequest / resumeRun shapes ────────────────────

export interface ExecuteRequestBody {
  task: string;
  role?: string;
  context?: string;
  outputInstruction?: string;
  tools?: string[];
  model?: string;
  max_tool_calls?: number;
}

export interface ExecuteRequestResponse {
  success: boolean;
  response?: string;
  error?: string;
  awaitsResponse: boolean;
  executionGuid?: string;
  stateGuid?: string;
  agui_tool_calls?: {
    tool_call_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
  }[];
}

export interface ResumeRunBody {
  executionGuid: string;
  stateGuid: string;
  toolCallId: string;
  result?: unknown;
  error?: string;
}
