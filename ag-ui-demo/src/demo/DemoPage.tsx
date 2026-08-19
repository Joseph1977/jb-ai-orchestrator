// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useRef, useState } from 'react';
import Checklist from '../components/Checklist/Checklist';
import type { ChecklistStep } from '../components/Checklist/types';
import OutputMessage from '../components/OutputMessage/OutputMessage';
import InputMessage from '../components/InputMessage/InputMessage';
import type { InputMode } from '../components/InputMessage/InputMessage';
import ApprovalPrompt from '../components/ApprovalPrompt/ApprovalPrompt';
import {
  registerTools,
  startRun,
  submitToolResponse,
  closeThread,
  DEFAULT_THREAD_ID,
} from '../api/agui';
import {
  buildConfiguredRunBindings,
  BindingConfigurationError,
} from '../config/bindings';
import { useGlobalSSE } from '../hooks/useSSE';
import type { AGUIEvent, FrontendTool, AGUIRunMessage } from '../types/agui';
import { EventType } from '../types/agui';

// ── frontend tool definitions ────────────────────────────

const FRONTEND_TOOLS: FrontendTool[] = [
  {
    name: 'Checklist-Update',
    description: 'Update the progress checklist shown to the user',
    parameters: {
      type: 'object',
      properties: {
        steps: {
          type: 'string',
          description:
            'JSON-encoded array of {id, title, status} where status is pending|active|done',
        },
      },
      required: ['steps'],
    },
  },
  {
    name: 'Show-Message',
    description: 'Display an info or warning message to the user',
    parameters: {
      type: 'object',
      properties: {
        type: { type: 'string', enum: ['info', 'warning'], description: 'Message severity' },
        title: { type: 'string', description: 'Optional title' },
        body: { type: 'string', description: 'Message body text' },
      },
      required: ['type', 'body'],
    },
  },
  {
    name: 'Ask-User',
    description: 'Prompt the user for input (yes/no, multi-checkbox, or table)',
    parameters: {
      type: 'object',
      properties: {
        mode: { type: 'string', enum: ['yesno', 'checkbox', 'table'], description: 'Input mode' },
        question: { type: 'string', description: 'The question to ask' },
        options: {
          type: 'string',
          description: 'JSON-encoded options array for checkbox mode',
        },
        columns: {
          type: 'string',
          description: 'JSON-encoded columns array for table mode',
        },
      },
      required: ['mode', 'question'],
    },
    extensions: { awaitsResponse: true },
  },
  {
    name: 'Request-Approval',
    description: 'Ask the user to approve or decline an action',
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'Action title' },
        description: { type: 'string', description: 'Action description' },
      },
      required: ['title'],
    },
    extensions: { awaitsResponse: true },
  },
];

const DEFAULT_PROMPT =
  'Show a 3-step checklist (Analyze → Process → Complete), display an info message, ' +
  'then ask me a yes/no question and wait for my answer, then request my approval.';

// ── log entry type ───────────────────────────────────────

interface LogEntry {
  id: number;
  ts: string;
  text: string;
  type: 'event' | 'send' | 'receive' | 'error';
}

// ── component ────────────────────────────────────────────

export default function DemoPage() {
  // UI state
  const [steps, setSteps] = useState<ChecklistStep[]>([]);
  const [messages, setMessages] = useState<
    { id: number; kind: 'info' | 'warning'; title?: string; body: string }[]
  >([]);
  const [pendingInput, setPendingInput] = useState<{
    toolCallId: string;
    input: InputMode;
  } | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    toolCallId: string;
    title: string;
    description?: string;
  } | null>(null);
  const [textStream, setTextStream] = useState('');
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [running, setRunning] = useState(false);
  const [closing, setClosing] = useState(false);
  const [sseEnabled, setSSEEnabled] = useState(false);
  const [fullLog, setFullLog] = useState(false);
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [model, setModel] = useState('gpt-4.1-mini');

  const logId = useRef(0);
  const toolArgBuffers = useRef<Map<string, string>>(new Map());
  const msgId = useRef(0);
  const fullLogRef = useRef(fullLog);
  fullLogRef.current = fullLog;

  // ── logging ────────────────────────────────────────────

  const log = useCallback((text: string, type: LogEntry['type'] = 'event') => {
    setLogs((prev) => [
      { id: ++logId.current, ts: new Date().toLocaleTimeString(), text, type },
      ...prev,
    ]);
  }, []);

  const logVerbose = useCallback(
    (text: string, type: LogEntry['type'] = 'event') => {
      if (fullLogRef.current) log(text, type);
    },
    [log],
  );

  // ── handle an AG-UI event ──────────────────────────────

  const handleEvent = useCallback(
    (event: AGUIEvent) => {
      if (fullLogRef.current) {
        log(`⬇ RECV ${event.type}: ${JSON.stringify(event)}`, 'receive');
      } else {
        log(`${event.type}`, 'receive');
      }

      switch (event.type) {
        case EventType.TOOL_CALL_START:
          toolArgBuffers.current.set(event.toolCallId, '');
          log(`Tool call started: ${event.toolCallName} (${event.toolCallId})`);
          break;

        case EventType.TOOL_CALL_ARGS: {
          const prev = toolArgBuffers.current.get(event.toolCallId) ?? '';
          toolArgBuffers.current.set(event.toolCallId, prev + event.delta);
          break;
        }

        case EventType.TOOL_CALL_END: {
          const raw = toolArgBuffers.current.get(event.toolCallId) ?? '{}';
          toolArgBuffers.current.delete(event.toolCallId);
          let args: Record<string, unknown> = {};
          try {
            args = JSON.parse(raw);
          } catch {
            log(`Failed to parse tool args: ${raw}`, 'error');
            break;
          }
          log(`Tool call complete: ${event.toolCallId} → ${JSON.stringify(args)}`);
          dispatchToolCall(event.toolCallId, args);
          break;
        }

        case EventType.TEXT_MESSAGE_START:
          setTextStream('');
          break;

        case EventType.TEXT_MESSAGE_CONTENT:
          setTextStream((prev) => prev + event.delta);
          break;

        case EventType.TEXT_MESSAGE_END:
          break;

        case EventType.RUN_FINISHED:
          setRunning(false);
          log('Run finished ✓');
          break;

        case EventType.RUN_ERROR:
          setRunning(false);
          log(`Run error: ${event.message}`, 'error');
          break;
      }
    },
    [log],
  );

  // ── dispatch a completed tool call by name ─────────────

  function dispatchToolCall(toolCallId: string, args: Record<string, unknown>) {
    // Checklist-Update
    if ('steps' in args) {
      try {
        const parsed =
          typeof args.steps === 'string' ? JSON.parse(args.steps as string) : args.steps;
        setSteps(parsed as ChecklistStep[]);
      } catch {
        log('Could not parse Checklist steps', 'error');
      }
      return;
    }

    // Show-Message
    if ('body' in args && 'type' in args) {
      const kind = args.type === 'warning' ? 'warning' : 'info';
      setMessages((prev) => [
        ...prev,
        {
          id: ++msgId.current,
          kind: kind as 'info' | 'warning',
          title: (args.title as string) ?? undefined,
          body: args.body as string,
        },
      ]);
      return;
    }

    // Ask-User (awaitsResponse)
    if ('mode' in args && 'question' in args) {
      const mode = args.mode as string;
      let input: InputMode;
      if (mode === 'checkbox') {
        let options: { label: string; value: string; checked?: boolean }[] = [];
        try {
          options =
            typeof args.options === 'string'
              ? JSON.parse(args.options as string)
              : (args.options as typeof options) ?? [];
        } catch { /* empty */ }
        input = { mode: 'checkbox', question: args.question as string, options };
      } else if (mode === 'table') {
        let columns: { key: string; label: string }[] = [];
        try {
          columns =
            typeof args.columns === 'string'
              ? JSON.parse(args.columns as string)
              : (args.columns as typeof columns) ?? [];
        } catch { /* empty */ }
        input = { mode: 'table', question: args.question as string, columns };
      } else {
        input = { mode: 'yesno', question: args.question as string };
      }
      setPendingInput({ toolCallId, input });
      return;
    }

    // Request-Approval (awaitsResponse)
    if ('title' in args) {
      setPendingApproval({
        toolCallId,
        title: args.title as string,
        description: (args.description as string) ?? undefined,
      });
      return;
    }

    log(`Unknown tool call args: ${JSON.stringify(args)}`, 'error');
  }

  // ── global SSE listener ────────────────────────────────

  const { connected: sseConnected } = useGlobalSSE(handleEvent, sseEnabled);

  // ── actions ────────────────────────────────────────────

  async function handleRegisterTools() {
    const payload = {
      threadId: DEFAULT_THREAD_ID,
      runId: '(auto)',
      messages: [],
      frontendTools: FRONTEND_TOOLS,
    };
    log(`⬆ SEND POST /api/ag-ui/run (bind-only)`, 'send');
    logVerbose(`Payload: ${JSON.stringify(payload, null, 2)}`, 'send');
    try {
      const events = await registerTools(FRONTEND_TOOLS);
      events.forEach((e) => {
        if (fullLogRef.current) {
          log(`⬇ RECV: ${JSON.stringify(e)}`, 'receive');
        } else {
          log(`bind: ${e.type}`, 'receive');
        }
      });
      log('Tools registered ✓');
    } catch (err) {
      log(`Register failed: ${err}`, 'error');
    }
  }

  async function handleStartRun() {
    let bindings;
    try {
      bindings = buildConfiguredRunBindings();
    } catch (err) {
      const message =
        err instanceof BindingConfigurationError
          ? err.message
          : 'Invalid binding configuration';
      log(`Binding config error: ${message}`, 'error');
      return;
    }

    const userMsg: AGUIRunMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: prompt,
    };
    const payload = {
      threadId: DEFAULT_THREAD_ID,
      runId: '(auto)',
      model,
      messages: [userMsg],
      frontendTools: FRONTEND_TOOLS.map((t) => t.name),
      ...(bindings ? { input: bindings.input, output: bindings.output, mode: bindings.mode } : {}),
    };
    setRunning(true);
    setTextStream('');
    log(`⬆ SEND POST /api/ag-ui/run`, 'send');
    log(`Prompt: "${prompt}"`, 'send');
    log(`Model: ${model}`, 'send');
    if (bindings) {
      log(`Bindings: input=${bindings.input.uri} mode=${bindings.mode ?? '(none)'}`, 'send');
      if (bindings.output) {
        log(`Bindings: output=${bindings.output.uri} (no credentials)`, 'send');
      }
    } else {
      log('Bindings: none (plain chat — no input/output env)', 'send');
    }
    logVerbose(`Full payload: ${JSON.stringify(payload, null, 2)}`, 'send');
    await startRun([userMsg], FRONTEND_TOOLS, {
      onEvent: handleEvent,
      onError: (err) => {
        setRunning(false);
        log(`Stream error: ${err.message}`, 'error');
      },
      onDone: () => setRunning(false),
    }, { model, bindings, threadId: DEFAULT_THREAD_ID });
  }

  async function handleCloseThread() {
    setClosing(true);
    log(`⬆ SEND POST /api/ag-ui/threads/${DEFAULT_THREAD_ID}/close`, 'send');
    try {
      const result = await closeThread({ threadId: DEFAULT_THREAD_ID });
      log(
        `Thread close: status=${result.status} alreadyClosed=${result.alreadyClosed} ` +
          `discardedHolds=${result.discardedHolds} runtimeDeleted=${result.runtimeDeleted}`,
        'receive',
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      log(`Close failed: ${message}`, 'error');
    } finally {
      setClosing(false);
    }
  }

  async function handleSubmitInput(result: unknown) {
    if (!pendingInput) return;
    log(`⬆ SEND POST /api/ag-ui/run (tool response)`, 'send');
    log(`toolCallId: ${pendingInput.toolCallId}`, 'send');
    logVerbose(`result: ${JSON.stringify(result)}`, 'send');
    try {
      const events = await submitToolResponse(pendingInput.toolCallId, result);
      events.forEach((e) => {
        if (fullLogRef.current) {
          log(`⬇ RECV: ${JSON.stringify(e)}`, 'receive');
        }
      });
      log('Input response submitted ✓');
    } catch (err) {
      log(`Submit failed: ${err}`, 'error');
    }
    setPendingInput(null);
  }

  async function handleApprovalDecision(decision: { approved: boolean; reason?: string }) {
    if (!pendingApproval) return;
    log(`⬆ SEND POST /api/ag-ui/run (approval response)`, 'send');
    log(`toolCallId: ${pendingApproval.toolCallId}`, 'send');
    logVerbose(`decision: ${JSON.stringify(decision)}`, 'send');
    try {
      const events = await submitToolResponse(pendingApproval.toolCallId, decision);
      events.forEach((e) => {
        if (fullLogRef.current) {
          log(`⬇ RECV: ${JSON.stringify(e)}`, 'receive');
        }
      });
      log('Approval submitted ✓');
    } catch (err) {
      log(`Submit failed: ${err}`, 'error');
    }
    setPendingApproval(null);
  }

  // ── render ─────────────────────────────────────────────

  return (
    <div className="demo-page">
      {/* toolbar */}
      <header className="demo-toolbar">
        <h1>AG-UI Demo</h1>
        <div className="demo-toolbar__actions">
          <button className="btn btn--primary" onClick={handleRegisterTools} title="POST /api/ag-ui/run (bind-only) — sends frontendTools definitions to the agent so the LLM knows which UI components it can call. No LLM invocation; just caches the tool list.">
            1 — Register Tools
          </button>
          <button
            className="btn btn--primary"
            onClick={handleStartRun}
            disabled={running}
            title="POST /api/ag-ui/run with your prompt and model — opens an SSE stream to receive TOOL_CALL_*, TEXT_MESSAGE_*, and lifecycle events from the LLM in real time."
          >
            {running ? 'Running…' : '2 — Start Run'}
          </button>
          <button
            className="btn btn--secondary"
            onClick={handleCloseThread}
            disabled={closing}
            title="POST /api/ag-ui/threads/{threadId}/close — returns 200 closed or retries 202 closing with bounded backoff."
          >
            {closing ? 'Closing…' : 'Close Thread'}
          </button>
          <label className="demo-toolbar__toggle" title="Subscribe to GET /api/ag-ui/events — a global fan-out SSE stream that mirrors all tool-call events from any run or executeRequest. Useful for debugging or dashboards.">
            <input
              type="checkbox"
              checked={sseEnabled}
              onChange={(e) => setSSEEnabled(e.target.checked)}
            />
            Global SSE {sseConnected ? '🟢' : '⚪'}
          </label>
          <label className="demo-toolbar__toggle" title="Show full request/response payloads in the event log">
            <input
              type="checkbox"
              checked={fullLog}
              onChange={(e) => setFullLog(e.target.checked)}
            />
            Full Log
          </label>
        </div>
      </header>

      {/* prompt & model config */}
      <section className="demo-config">
        <div className="demo-config__field">
          <label htmlFor="model-input">Model</label>
          <input
            id="model-input"
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="e.g. gpt-4.1-mini, gpt-4o, claude-3-5-sonnet"
            className="demo-config__input"
            title="LiteLLM model identifier sent in the run request. Must match a model configured in your LiteLLM proxy."
          />
        </div>
        <div className="demo-config__field demo-config__field--wide">
          <label htmlFor="prompt-input">Prompt</label>
          <textarea
            id="prompt-input"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={3}
            className="demo-config__textarea"
            placeholder="Enter the message to send to the LLM…"
            title="The user message sent to the LLM. The LLM will read this along with the registered tool definitions and decide which tools to call. Edit freely before clicking Start Run."
          />
        </div>
      </section>

      <div className="demo-grid">
        {/* left: interactive widgets */}
        <section className="demo-panel">
          <h2>Checklist</h2>
          {steps.length > 0 ? (
            <Checklist steps={steps} />
          ) : (
            <p className="demo-empty">No steps yet — start a run.</p>
          )}

          <h2>Messages</h2>
          {messages.length > 0 ? (
            messages.map((m) => (
              <OutputMessage key={m.id} type={m.kind} title={m.title} body={m.body} />
            ))
          ) : (
            <p className="demo-empty">No messages.</p>
          )}

          {textStream && (
            <>
              <h2>Assistant</h2>
              <div className="demo-text-stream">{textStream}</div>
            </>
          )}

          {pendingInput && (
            <>
              <h2>Input Required</h2>
              <InputMessage input={pendingInput.input} onSubmit={handleSubmitInput} />
            </>
          )}

          {pendingApproval && (
            <>
              <h2>Approval Required</h2>
              <ApprovalPrompt
                title={pendingApproval.title}
                description={pendingApproval.description}
                onDecision={handleApprovalDecision}
              />
            </>
          )}
        </section>

        {/* right: event log */}
        <section className="demo-panel demo-panel--log">
          <div className="demo-log-header">
            <h2>Event Log</h2>
            <button className="btn btn--secondary btn--sm" onClick={() => setLogs([])} title="Clear all entries from the event log">
              Clear
            </button>
          </div>
          <div className="demo-log">
            {logs.map((l) => (
              <div key={l.id} className={`demo-log__entry demo-log__entry--${l.type}`}>
                <span className="demo-log__ts">{l.ts}</span>
                <span>{l.text}</span>
              </div>
            ))}
            {logs.length === 0 && <p className="demo-empty">No events yet.</p>}
          </div>
        </section>
      </div>
    </div>
  );
}
