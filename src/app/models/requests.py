# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from typing import Optional, List, Any, Dict
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.bindings import ExecutionMode, LocationBinding, TransientCredentials
from app.models.execution_models import ExecutionStatus

class ExecuteRequestInput(BaseModel):
    role: Optional[str] = None
    task: str  # Required field
    context: Optional[str] = None
    outputInstruction: Optional[str] = None
    tools: Optional[List[str]] = None
    model: Optional[str] = "gpt-3.5-turbo"
    max_tool_calls: Optional[int] = 10
    llmRequestTimeoutInSec: Optional[int] = None

class ToolInfo(BaseModel):
    name: str
    description: str
    input_schema: dict
    server_url: Optional[str] = None
    server_id: Optional[str] = None
    original_name: Optional[str] = None

class ToolCallInfo(BaseModel):
    tool_index: int
    tool_name: str
    llm_tool_interaction_index: int
    mcp_server_id: Optional[str] = None
    mcp_server_url: Optional[str] = None
    tool_source: Optional[str] = "MCP"
    agui_original_name: Optional[str] = None

class AGUIToolCall(BaseModel):
    tool_call_id: str
    tool_name: str
    arguments: dict

class ExecuteRequestResponse(BaseModel):
    success: bool
    response: Optional[str] = None
    error: Optional[str] = None
    tool_calls_made: Optional[int] = 0
    total_tokens: Optional[int] = 0
    promptTokenCount: Optional[int] = 0
    candidatesTokenCount: Optional[int] = 0
    partial_response: Optional[str] = None
    tool_calls_info: Optional[List[ToolCallInfo]] = []
    agui_tool_calls: Optional[List[AGUIToolCall]] = []
    executionGuid: Optional[UUID] = None
    executionStatus: Optional[ExecutionStatus] = None
    awaitsResponse: Optional[bool] = False
    stateGuid: Optional[UUID] = None
    interrupts: Optional[List[Dict[str, Any]]] = None
    pendingToolCallIds: Optional[List[str]] = None
    errorCode: Optional[str] = None

class GetToolsResponse(BaseModel):
    success: bool
    tools: List[ToolInfo] = []
    error: Optional[str] = None


class ResumeRunInput(BaseModel):
    executionGuid: UUID
    stateGuid: UUID
    toolCallId: str
    result: Optional[Any] = None
    error: Optional[str] = None


class ExecutionStatusResponse(BaseModel):
    executionGuid: UUID
    status: ExecutionStatus
    result: Optional[dict] = None
    error: Optional[str] = None
    awaitsResponse: bool = False
    stateGuid: Optional[UUID] = None
    closeRequestedAt: Optional[datetime] = None
    closedAt: Optional[datetime] = None


class OrchestratorCloseResponse(BaseModel):
    model_config = {"populate_by_name": True}

    status: str
    already_closed: bool = Field(alias="alreadyClosed")
    discarded_holds: int = Field(alias="discardedHolds")
    runtime_deleted: bool = Field(alias="runtimeDeleted")
    workspace_deleted: bool = Field(alias="workspaceDeleted")
    workspace_deleted_count: int = Field(alias="workspaceDeletedCount")


# ---------------------------------------------------------------------------
# Orchestrator (folder execution) models
# ---------------------------------------------------------------------------

class InitiateOrchestratorInput(BaseModel):
    """Config/bind step: point the harness at a folder. No LLM call."""

    folder: Optional[str] = None  # legacy: git URL, archive URL, or local path
    input: Optional[LocationBinding] = None
    output: Optional[LocationBinding] = None
    mode: Optional[ExecutionMode] = None
    orchestrationType: Optional[str] = None  # cursor | claude-code | ... | None=auto
    systemContext: Optional[str] = None  # injected into every LLM iteration
    model: Optional[str] = None  # default model for subsequent execute calls
    maxToolCalls: Optional[int] = None
    inPlace: Optional[bool] = False  # legacy writable in-place
    credentials: Optional[TransientCredentials] = None


class PrimitiveSummary(BaseModel):
    name: str
    path: str
    description: str = ""
    kind: str = "skill"


class InitiateOrchestratorResponse(BaseModel):
    success: bool
    orchestratorGuid: Optional[UUID] = None
    orchestrationType: Optional[str] = None
    detected: bool = False
    confidence: int = 0
    workspacePath: Optional[str] = None
    sourceKind: Optional[str] = None
    input: Optional[dict] = None
    output: Optional[dict] = None
    mode: Optional[str] = None
    agents: List[PrimitiveSummary] = Field(default_factory=list)
    skills: List[PrimitiveSummary] = Field(default_factory=list)
    rules: List[PrimitiveSummary] = Field(default_factory=list)
    commands: List[PrimitiveSummary] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    errorCode: Optional[str] = None


class ExecuteOrchestratorInput(BaseModel):
    """Run step: send a prompt against an initiated orchestrator session."""

    orchestratorGuid: UUID
    prompt: str
    model: Optional[str] = None
    maxToolCalls: Optional[int] = None
    tools: Optional[List[str]] = None
    # AG-UI interaction channel is opt-in and scoped per call/session.
    threadId: Optional[str] = None
    runId: Optional[str] = None
    frontendTools: Optional[List[Dict[str, Any]]] = None
    credentials: Optional[TransientCredentials] = None


class OrchestratorResumeInput(BaseModel):
    """Resume an orchestrator run awaiting user/tool input (pod-agnostic)."""

    orchestratorGuid: UUID
    stateGuid: UUID
    toolCallId: str
    result: Optional[Any] = None
    error: Optional[str] = None
    credentials: Optional[TransientCredentials] = None
