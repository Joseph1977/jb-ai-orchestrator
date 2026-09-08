# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import uuid
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.db.session import get_session
from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.models.requests import (
    ExecuteRequestInput,
    ExecuteRequestResponse,
    ExecutionStatusResponse,
    GetToolsResponse,
    ResumeRunInput,
    ToolInfo,
)
from app.services.mcp_agent_service import MCPAgentService
from app.services.agui_service import agui_service
from app.services.agui_event_service import agui_event_service
from app.services.resume_options import (
    resolve_resume_max_tool_calls,
    resolve_resume_model,
)
from app.services.tool_hub import ToolExecutionHub
from app.services.execution_state_service import execution_state_service
from app.utils.logger import logger

router = APIRouter(prefix="/agent", tags=["Agent"])

# Initialize the MCP Agent Service - will be set during startup
mcp_service = None
tool_hub = None

def initialize_mcp_service():
    """Initialize the MCP service after configuration is loaded"""
    global mcp_service, tool_hub
    mcp_service = MCPAgentService()
    tool_hub = ToolExecutionHub(
        mcp_service=mcp_service,
        agui_service=agui_service,
        agui_event_service=agui_event_service,
    )
    return mcp_service

def get_mcp_service() -> MCPAgentService:
    """Helper to retrieve the initialized MCP service instance."""
    global mcp_service
    if mcp_service is None:
        raise HTTPException(status_code=500, detail="MCP service not initialized")
    return mcp_service

def get_tool_hub() -> ToolExecutionHub:
    global tool_hub
    if tool_hub is None:
        raise HTTPException(status_code=500, detail="Tool hub not initialized")
    return tool_hub


def _build_execute_response(
    *,
    execution_id: uuid.UUID,
    status: ExecutionStatus,
    result: dict,
    state_id: Optional[uuid.UUID] = None,
) -> dict:
    payload = {
        "success": result.get("success", False),
        "response": result.get("response"),
        "error": result.get("error"),
        "tool_calls_made": result.get("tool_calls_made", 0),
        "total_tokens": result.get("total_tokens", 0),
        "promptTokenCount": result.get("prompt_tokens", 0),
        "candidatesTokenCount": result.get("completion_tokens", 0),
        "partial_response": result.get("partial_response"),
        "tool_calls_info": result.get("tool_calls_info") or [],
    }

    agui_tool_calls = result.get("agui_tool_calls")
    if agui_tool_calls:
        payload["agui_tool_calls"] = agui_tool_calls

    if status == ExecutionStatus.AWAITING_RESPONSE:
        payload.update(
            {
                "executionGuid": str(execution_id),
                "executionStatus": status.value,
                "awaitsResponse": True,
            }
        )
        if state_id:
            payload["stateGuid"] = str(state_id)

    return payload


async def _create_execution_record() -> uuid.UUID:
    async with get_session() as session:
        execution = await execution_state_service.create_execution(
            session, status=ExecutionStatus.RUNNING
        )
        return execution.id


async def _update_execution_record(
    execution_id: uuid.UUID,
    *,
    status: Optional[ExecutionStatus] = None,
    result: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    async with get_session() as session:
        await execution_state_service.update_execution(
            session,
            execution_id,
            status=status,
            result=result,
            error_message=error,
        )


async def _create_state_for_execution(
    execution_id: uuid.UUID,
    *,
    state_payload: dict,
    pending_tool: dict,
) -> uuid.UUID:
    async with get_session() as session:
        state = await execution_state_service.create_state(
            session,
            execution_id=execution_id,
            payload=state_payload,
            status=LLMStateStatus.AWAITING_RESPONSE,
            thread_id=None,
            run_id=None,
            tool_call_id=pending_tool.get("tool_call_id"),
        )
        await execution_state_service.update_execution(
            session,
            execution_id,
            status=ExecutionStatus.AWAITING_RESPONSE,
        )
        return state.id


async def _mark_state_status(state_id: uuid.UUID, status: LLMStateStatus) -> None:
    async with get_session() as session:
        await execution_state_service.mark_state_status(session, state_id, status)


@router.post("/executeRequest", response_model=ExecuteRequestResponse)
async def execute_request(request: ExecuteRequestInput):
    """Execute a request using LiteLLM, optionally with MCP tools."""
    tool_execution_hub = get_tool_hub()
    execution_id = await _create_execution_record()

    logger.info("Executing request: %s (execution=%s)", request.task, execution_id)
    logger.info("Requested tools: %s", request.tools)
    logger.info("Model: %s", request.model)

    prompt_parts: List[str] = []
    if request.role:
        prompt_parts.append(f"Role: {request.role}")
    prompt_parts.append(f"Task: {request.task}")
    if request.context:
        prompt_parts.append(f"Context: {request.context}")
    if request.outputInstruction:
        prompt_parts.append(f"Output Instructions: {request.outputInstruction}")
    full_prompt = "\n\n".join(prompt_parts)

    try:
        result = await tool_execution_hub.process_request(
            request=full_prompt,
            model=request.model or "gpt-3.5-turbo",
            max_tool_calls=request.max_tool_calls,
            requested_tools=request.tools,
            lite_llm_request_timeout_in_sec=request.llmRequestTimeoutInSec,
            include_agui_tools=True,
        )

        if result.get("awaits_response"):
            state_payload = result.get("state")
            pending_tool = result.get("pending_tool") or {}
            if not state_payload:
                raise HTTPException(status_code=500, detail="Awaiting-response state missing payload")

            state_id = await _create_state_for_execution(
                execution_id,
                state_payload=state_payload,
                pending_tool=pending_tool,
            )
            logger.info(
                "Execution %s awaiting AG-UI response (state=%s tool_call=%s)",
                execution_id,
                state_id,
                pending_tool.get("tool_call_id"),
            )
            return JSONResponse(
                content=_build_execute_response(
                    execution_id=execution_id,
                    status=ExecutionStatus.AWAITING_RESPONSE,
                    result=result,
                    state_id=state_id,
                )
            )

        status = ExecutionStatus.COMPLETED if result.get("success") else ExecutionStatus.FAILED
        await _update_execution_record(
            execution_id,
            status=status,
            result=result if result.get("success") else None,
            error=None if result.get("success") else result.get("error"),
        )
        logger.info("Execution %s finished with status %s", execution_id, status)
        return JSONResponse(
            content=_build_execute_response(
                execution_id=execution_id,
                status=status,
                result=result,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to execute request %s: %s", execution_id, exc)
        await _update_execution_record(
            execution_id,
            status=ExecutionStatus.FAILED,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"Failed to execute request: {str(exc)}")


@router.post("/resumeRun", response_model=ExecuteRequestResponse)
async def resume_run(payload: ResumeRunInput):
    tool_execution_hub = get_tool_hub()
    execution_id = payload.executionGuid
    state_id = payload.stateGuid

    async with get_session() as session:
        execution = await execution_state_service.get_execution(session, execution_id)
        if not execution:
            raise HTTPException(status_code=404, detail="Execution not found")
        state = await execution_state_service.get_state(session, state_id)
        if not state or state.execution_id != execution_id:
            raise HTTPException(status_code=404, detail="LLM state not found for execution")
        state_payload = state.state_payload
        state_status = state.status

    if state_status != LLMStateStatus.AWAITING_RESPONSE:
        raise HTTPException(status_code=400, detail="State is not awaiting a response")

    pending_tool = state_payload.get("pending_tool") or {}
    if payload.toolCallId != pending_tool.get("tool_call_id"):
        raise HTTPException(status_code=400, detail="toolCallId does not match pending tool")

    tool_result = payload.result or {}
    if payload.error:
        tool_result["error"] = payload.error

    resolved_model = resolve_resume_model(
        payload.model,
        state_payload.get("model"),
    )
    resolved_max_calls = resolve_resume_max_tool_calls(
        payload.max_tool_calls,
        state_payload.get("max_calls"),
    )

    try:
        result = await tool_execution_hub.process_request(
            request=state_payload.get("request", ""),
            model=resolved_model,
            max_tool_calls=resolved_max_calls,
            requested_tools=state_payload.get("requested_tools"),
            lite_llm_request_timeout_in_sec=state_payload.get("lite_llm_request_timeout_in_sec"),
            resume_state=state_payload,
            resume_tool_result=tool_result,
        )
    except HTTPException:
        raise
    except Exception as exc:
        await _mark_state_status(state_id, LLMStateStatus.DISCARDED)
        await _update_execution_record(execution_id, status=ExecutionStatus.FAILED, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to resume run: {str(exc)}")

    await _mark_state_status(state_id, LLMStateStatus.COMPLETED)

    if result.get("awaits_response"):
        state_payload = result.get("state")
        pending_tool = result.get("pending_tool") or {}
        if not state_payload:
            raise HTTPException(status_code=500, detail="Awaiting-response state missing payload")
        new_state_id = await _create_state_for_execution(
            execution_id,
            state_payload=state_payload,
            pending_tool=pending_tool,
        )
        return JSONResponse(
            content=_build_execute_response(
                execution_id=execution_id,
                status=ExecutionStatus.AWAITING_RESPONSE,
                result=result,
                state_id=new_state_id,
            )
        )

    status = ExecutionStatus.COMPLETED if result.get("success") else ExecutionStatus.FAILED
    await _update_execution_record(
        execution_id,
        status=status,
        result=result if result.get("success") else None,
        error=None if result.get("success") else result.get("error"),
    )
    return JSONResponse(
        content=_build_execute_response(
            execution_id=execution_id,
            status=status,
            result=result,
        )
    )


@router.get("/execution/{execution_guid}", response_model=ExecutionStatusResponse)
async def get_execution_status(execution_guid: UUID):
    async with get_session() as session:
        execution = await execution_state_service.get_execution(session, execution_guid)
        if not execution:
            raise HTTPException(status_code=404, detail="Execution not found")
        latest_state = await execution_state_service.get_latest_state_for_execution(session, execution_guid)

    awaits_response = execution.status == ExecutionStatus.AWAITING_RESPONSE
    state_id = None
    if awaits_response and latest_state and latest_state.status == LLMStateStatus.AWAITING_RESPONSE:
        state_id = latest_state.id

    return ExecutionStatusResponse(
        executionGuid=execution_guid,
        status=execution.status,
        result=execution.result,
        error=execution.error_message,
        awaitsResponse=awaits_response,
        stateGuid=state_id,
    )

@router.get("/getTools", response_model=GetToolsResponse)
async def get_tools():
    """
    Get all available MCP tools

    Returns:
        GetToolsResponse: Response with list of available tools and their schemas
    """
    try:
        global mcp_service
        if mcp_service is None:
            raise HTTPException(status_code=500, detail="MCP service not initialized")

        logger.info("Fetching available MCP tools")

        # Fetch tools from the MCP server
        mcp_tools = await mcp_service.fetch_mcp_tools()

        # Convert to response format (MCP tools)
        tools = [
            ToolInfo(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                server_url=getattr(tool, 'server_url', None),
                server_id=getattr(tool, 'server_name', None),
                original_name=getattr(tool, 'original_name', None)
            )
            for tool in mcp_tools
        ]

        # Include AG-UI tools (global list provided by the UI-BE)
        agui_tools = agui_service.list_tools()
        for tool in agui_tools:
            tools.append(
                ToolInfo(
                    name=tool.prefixed_name or tool.name,
                    description=f"[AG-UI] {tool.description}",
                    input_schema=tool.parameters,
                    server_url="AG-UI",
                    server_id="AG-UI",
                    original_name=tool.original_name
                )
            )

        response = GetToolsResponse(
            success=True,
            tools=tools
        )

        logger.info(f"Successfully fetched {len(tools)} tools")
        return response

    except Exception as e:
        logger.error(f"Failed to fetch tools: {str(e)}")
        return GetToolsResponse(
            success=False,
            tools=[],
            error=f"Failed to fetch tools: {str(e)}"
        )
