# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.config import Config
from app.services.agui_messages import dedupe_litellm_tool_results
from app.utils.logger import logger


class LLMUpstreamError(Exception):
    """A provider failure safe to propagate beyond the orchestrator boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _llm_upstream_error(status_code: int) -> LLMUpstreamError:
    if status_code in {408, 504}:
        return LLMUpstreamError("TIMEOUT", "Model request timed out")
    if status_code == 402:
        return LLMUpstreamError("QUOTA", "Model service quota exceeded")
    if status_code == 429:
        return LLMUpstreamError("RATE_LIMIT", "Model service rate limit exceeded")
    if status_code in {401, 403}:
        return LLMUpstreamError("AUTH", "Model service authentication failed")
    return LLMUpstreamError("UNAVAILABLE", "Model service is unavailable")


def _check_output_limit(result: dict) -> None:
    """Reject truncated model output before callers route tool calls."""
    choices = result.get("choices") or []
    if not choices:
        return
    finish_reason = choices[0].get("finish_reason")
    if finish_reason == "length":
        raise LLMUpstreamError("OUTPUT_LIMIT", "Model output limit reached")


def _safe_litellm_exception(exc: Exception) -> Exception:
    if isinstance(exc, LLMUpstreamError):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        return LLMUpstreamError("TIMEOUT", "Model request timed out")
    if isinstance(exc, httpx.TimeoutException):
        return LLMUpstreamError("TIMEOUT", "Model request timed out")
    if isinstance(exc, httpx.HTTPError):
        return LLMUpstreamError("UNAVAILABLE", "Model service is unavailable")
    return RuntimeError("Model request failed")


@dataclass
class MCPTool:
    """Represents an MCP tool with its schema"""
    name: str
    description: str
    input_schema: dict
    server_url: str
    server_name: str  # Human-readable server identifier (e.g., "search", "general")
    original_name: str  # Original tool name before any prefixing
    output_schema: Optional[dict] = None
    annotations: Optional[dict] = None
    meta: Optional[dict] = None


class MCPAgentService:
    """
    MCP Agent Service that integrates with multiple MCP servers and LiteLLM.

    This service:
    1. Connects to multiple MCP servers to fetch available tools
    2. Handles tool name conflicts by prefixing with server identifier
    3. Converts MCP tools to LiteLLM function format
    4. Calls LiteLLM with the tools and user request
    5. Executes tool calls on the appropriate MCP server
    6. Returns the final response
    """

    def __init__(
        self,
        mcp_server_configs: List[dict] = None,
        litellm_base_url: str = None,
        litellm_api_key: str = None,
        litellm_request_timeout_in_sec: int = None,
        litellm_drop_params: bool = None,
        litellm_model_deadline_sec: int = None,
        litellm_max_completion_tokens: int = None,
    ):
        # Initialize MCP servers
        if mcp_server_configs is not None:
            self.mcp_server_configs = mcp_server_configs
        else:
            self.mcp_server_configs = Config.MCP_SERVER_URLS or []

        # LiteLLM configuration
        self.litellm_base_url = litellm_base_url or Config.LITELLM_BASE_URL
        self.litellm_api_key = litellm_api_key or Config.LITELLM_API_KEY
        self.litellm_request_timeout_in_sec = litellm_request_timeout_in_sec or Config.LITELLM_REQUEST_TIMEOUT_IN_SEC
        self.litellm_drop_params = litellm_drop_params or Config.LITELLM_DROP_PARAMS
        self.litellm_model_deadline_sec = (
            litellm_model_deadline_sec
            if litellm_model_deadline_sec is not None
            else Config.LITELLM_MODEL_DEADLINE_SEC
        )
        configured_max_tokens = (
            litellm_max_completion_tokens
            if litellm_max_completion_tokens is not None
            else Config.LITELLM_MAX_COMPLETION_TOKENS
        )
        self.litellm_max_completion_tokens = configured_max_tokens

        # Generate server mapping: name -> config
        self.server_mapping = {}  # server_name -> config
        for config in self.mcp_server_configs:
            server_name = config['name']
            self.server_mapping[server_name] = config

        # Tool storage
        self.all_mcp_tools: List[MCPTool] = []

        logger.info(f"Initialized MCPAgentService with {len(self.mcp_server_configs)} MCP servers:")
        for config in self.mcp_server_configs:
            logger.info(f"  - {config['name']}: {config['url']}")
        logger.info(f"LiteLLM server: {self.litellm_base_url}")

    @staticmethod
    def _schema_to_dict(schema: Any) -> dict:
        """JSON-safe conversion for MCP inputSchema/outputSchema."""
        if schema is None:
            return {}
        if isinstance(schema, dict):
            return schema
        if hasattr(schema, "model_dump"):
            return schema.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(schema, "__dict__"):
            return {k: v for k, v in schema.__dict__.items() if not k.startswith("_")}
        try:
            return json.loads(json.dumps(schema, default=str))
        except (TypeError, ValueError):
            return {}

    def _validate_tool_args(self, schema: dict, arguments: dict) -> Optional[str]:
        """Validate args against inputSchema when jsonschema is available."""
        if not schema or not arguments:
            return None
        try:
            import jsonschema

            jsonschema.validate(instance=arguments, schema=schema)
        except ImportError:
            return None
        except Exception as exc:
            return f"Invalid tool arguments: {exc}"
        return None

    @staticmethod
    def _normalize_mcp_result(result) -> dict:
        """Stable internal MCP result preserving rich fields."""
        content_blocks = []
        for block in getattr(result, "content", None) or []:
            if hasattr(block, "model_dump"):
                content_blocks.append(block.model_dump(mode="json", by_alias=True, exclude_none=True))
            elif hasattr(block, "text"):
                content_blocks.append({"type": "text", "text": block.text or str(block)})
            else:
                content_blocks.append({"type": "text", "text": str(block)})

        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
        is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
        meta = getattr(result, "meta", None) or getattr(result, "metadata", None)
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump(mode="json", by_alias=True, exclude_none=True)

        text_parts = [
            b.get("text", "") for b in content_blocks if isinstance(b, dict) and b.get("type") == "text"
        ]
        primary_text = "\n".join(p for p in text_parts if p)

        out: dict = {
            "content": content_blocks,
            "structuredContent": structured,
            "isError": is_error,
            "metadata": meta,
        }
        if is_error:
            out["error"] = primary_text or "Tool execution error"
        elif primary_text:
            out["result"] = primary_text
        elif structured is not None:
            out["result"] = structured
        else:
            out["result"] = "Tool executed successfully but returned no content"
        return out


    def _generate_unique_tool_name(self, tool_name: str, server_name: str, existing_tools: List[MCPTool]) -> str:
        """Generate a unique tool name, adding server prefix if there are conflicts"""
        # Check if this tool name already exists
        existing_names = [tool.name for tool in existing_tools]

        if tool_name not in existing_names:
            return tool_name

        # Tool name conflicts, add server prefix
        return f"{server_name}-{tool_name}"

    async def fetch_mcp_tools_from_server(self, server_config: dict) -> List[MCPTool]:
        """Fetch available tools from a specific MCP server"""
        server_name = server_config['name']
        server_url = server_config['url']

        try:
            logger.info(f"Connecting to MCP server '{server_name}' at {server_url}")

            async with streamablehttp_client(server_url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    # Initialize the MCP session
                    logger.info(f"Initializing MCP session for '{server_name}'...")
                    await session.initialize()
                    logger.info(f"✅ MCP session for '{server_name}' initialized successfully!")

                    # List available tools (paginate when cursor supported).
                    logger.info(f"📋 Fetching available tools from '{server_name}'...")
                    mcp_tools = []
                    cursor = None
                    while True:
                        list_kwargs = {}
                        if cursor is not None:
                            list_kwargs["cursor"] = cursor
                        try:
                            tools_response = await session.list_tools(**list_kwargs)
                        except TypeError:
                            tools_response = await session.list_tools()
                            cursor = None
                        for tool in tools_response.tools:
                            input_schema = self._schema_to_dict(
                                getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
                            )
                            output_schema = self._schema_to_dict(
                                getattr(tool, "outputSchema", None) or getattr(tool, "output_schema", None)
                            ) or None
                            annotations = self._schema_to_dict(getattr(tool, "annotations", None)) or None
                            meta = self._schema_to_dict(getattr(tool, "meta", None)) or None

                            mcp_tool = MCPTool(
                                name=tool.name,
                                description=tool.description or "",
                                input_schema=input_schema,
                                server_url=server_url,
                                server_name=server_name,
                                original_name=tool.name,
                                output_schema=output_schema,
                                annotations=annotations,
                                meta=meta,
                            )
                            mcp_tools.append(mcp_tool)
                            logger.info(f"  - {server_name}: {tool.name}: {tool.description}")

                        cursor = getattr(tools_response, "nextCursor", None) or getattr(
                            tools_response, "next_cursor", None
                        )
                        if not cursor:
                            break

                    logger.info(f"✅ Found {len(mcp_tools)} tools from '{server_name}'")
                    return mcp_tools

        except Exception as e:
            logger.error(f"Failed to fetch MCP tools from '{server_name}' ({server_url}): {e}")
            return []  # Return empty list instead of raising to allow other servers to work

    async def fetch_mcp_tools(self) -> List[MCPTool]:
        """Fetch available tools from all MCP servers"""
        all_tools = []

        # Fetch tools from all servers
        for server_config in self.mcp_server_configs:
            tools = await self.fetch_mcp_tools_from_server(server_config)
            all_tools.extend(tools)

        # Resolve tool name conflicts
        resolved_tools = []
        tool_name_counts = {}

        # First pass: count tool name occurrences
        for tool in all_tools:
            tool_name_counts[tool.original_name] = tool_name_counts.get(tool.original_name, 0) + 1

        # Second pass: assign unique names with server attribution
        for tool in all_tools:
            # For multiple servers: ALWAYS include server name (even auto-generated ones like mcp1, mcp2)
            # For single server: only add server name if it's NOT "default"
            if len(self.mcp_server_configs) > 1:
                # Multiple servers: ALWAYS add server name
                tool.name = f"{tool.original_name}_{tool.server_name}"
                if tool_name_counts[tool.original_name] > 1:
                    logger.info(f"Tool name conflict resolved: '{tool.original_name}' from '{tool.server_name}' renamed to '{tool.name}'")
                else:
                    logger.info(f"Tool name enhanced with server attribution: '{tool.original_name}' from '{tool.server_name}' renamed to '{tool.name}'")
            elif tool.server_name != "default":
                # Single server with custom name: add server name
                tool.name = f"{tool.original_name}_{tool.server_name}"
                logger.info(f"Single server with custom name: '{tool.original_name}' from '{tool.server_name}' renamed to '{tool.name}'")
            else:
                # Single server with default name: keep original name
                tool.name = tool.original_name
                logger.info(f"Single server mode: keeping '{tool.original_name}' without server suffix")

            resolved_tools.append(tool)

        self.all_mcp_tools = resolved_tools
        logger.info(f"✅ Total tools available from all servers: {len(resolved_tools)}")

        # Log tool summary by server
        server_tool_counts = {}
        for tool in resolved_tools:
            server_tool_counts[tool.server_name] = server_tool_counts.get(tool.server_name, 0) + 1

        for server_name, count in server_tool_counts.items():
            logger.info(f"  - {server_name}: {count} tools")

        return resolved_tools

    def find_tool_by_name(self, tool_name: str) -> Optional[MCPTool]:
        """Find a tool by its name (which may include server suffix)"""
        for tool in self.all_mcp_tools:
            if tool.name == tool_name:
                return tool
        return None

    async def execute_mcp_tool(self, tool_name: str, arguments: dict) -> dict:
        """Execute a tool on the appropriate MCP server"""
        try:
            # Find the tool
            tool = self.find_tool_by_name(tool_name)
            if not tool:
                logger.error(f"Tool '{tool_name}' not found")
                return {"error": f"Tool '{tool_name}' not found", "isError": True}

            validation_error = self._validate_tool_args(tool.input_schema, arguments)
            if validation_error:
                return {"error": validation_error, "isError": True, "validationFailed": True}

            logger.info(f"Executing MCP tool: {tool_name} (original: {tool.original_name}) on '{tool.server_name}' with args: {arguments}")

            async with streamablehttp_client(tool.server_url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    # Call the tool using its original name on the server
                    result = await session.call_tool(tool.original_name, arguments)
                    response = self._normalize_mcp_result(result)
                    if response.get("isError"):
                        logger.warning(f"Tool {tool_name} returned isError on '{tool.server_name}'")
                    else:
                        logger.info(f"✅ Tool {tool_name} executed successfully on '{tool.server_name}'")
                    return response

        except Exception as e:
            logger.error(f"Failed to execute MCP tool {tool_name} on '{tool.server_name if 'tool' in locals() else 'unknown'}': {e}")
            return {"error": f"Tool execution failed: {str(e)}", "isError": True, "executionFailed": True}

    def convert_mcp_tools_to_litellm(self, mcp_tools: List[MCPTool]) -> List[dict]:
        """Convert MCP tools to LiteLLM tools format"""
        litellm_tools = []

        for tool in mcp_tools:
            # Enhance description to include server information
            enhanced_description = tool.description
            if hasattr(tool, 'server_name') and tool.server_name:
                enhanced_description = f"[{tool.server_name}] {tool.description}"

            # Convert MCP tool schema to LiteLLM tools format
            tool_def = {
                "type": "function",
                "function": {
                    "name": tool.name,  # This is the potentially renamed tool name
                    "description": enhanced_description,
                    "parameters": tool.input_schema if tool.input_schema else {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            }
            litellm_tools.append(tool_def)

        logger.info(f"Converted {len(litellm_tools)} MCP tools to LiteLLM tools format")
        return litellm_tools

    async def call_litellm(
        self,
        messages: List[dict],
        model: str = "gpt-3.5-turbo",
        tools: List[dict] = None,
        lite_llm_request_timeout_in_sec: int = None,
        *,
        stream: bool = False,
        on_content_delta=None,
        max_completion_tokens: Optional[int] = None,
        model_deadline_sec: Optional[int] = None,
    ) -> dict:
        """Call LiteLLM chat completions.

        When ``stream=True``, reads SSE chunks, optionally invokes
        ``on_content_delta(text_chunk)`` for each content token, and returns a
        reconstructed OpenAI-style completion dict (message + usage).

        An absolute asyncio deadline bounds the full call (including stream
        consumption). The httpx timeout remains a per-read idle/network guard.
        """
        deadline = (
            model_deadline_sec
            if model_deadline_sec is not None
            else self.litellm_model_deadline_sec
        )
        try:
            if deadline and deadline > 0:
                result = await asyncio.wait_for(
                    self._call_litellm_inner(
                        messages=messages,
                        model=model,
                        tools=tools,
                        lite_llm_request_timeout_in_sec=lite_llm_request_timeout_in_sec,
                        stream=stream,
                        on_content_delta=on_content_delta,
                        max_completion_tokens=max_completion_tokens,
                    ),
                    timeout=deadline,
                )
            else:
                result = await self._call_litellm_inner(
                    messages=messages,
                    model=model,
                    tools=tools,
                    lite_llm_request_timeout_in_sec=lite_llm_request_timeout_in_sec,
                    stream=stream,
                    on_content_delta=on_content_delta,
                    max_completion_tokens=max_completion_tokens,
                )
            _check_output_limit(result)
            return result
        except LLMUpstreamError as exc:
            logger.error("Failed to call LiteLLM: code=%s", exc.code)
            raise
        except asyncio.TimeoutError as exc:
            logger.error("LiteLLM call exceeded absolute deadline (%ss)", deadline)
            raise LLMUpstreamError("TIMEOUT", "Model request timed out") from exc
        except Exception as exc:
            logger.error(
                "Failed to call LiteLLM: type=%s",
                type(exc).__name__,
            )
            raise _safe_litellm_exception(exc) from exc

    async def _call_litellm_inner(
        self,
        *,
        messages: List[dict],
        model: str,
        tools: List[dict],
        lite_llm_request_timeout_in_sec: int,
        stream: bool,
        on_content_delta,
        max_completion_tokens: Optional[int],
    ) -> dict:
        try:
            messages, duplicate_tool_call_ids = dedupe_litellm_tool_results(messages)
            if duplicate_tool_call_ids:
                logger.warning(
                    "Normalized %d duplicate LiteLLM tool result(s) for tool_call_ids: %s",
                    len(duplicate_tool_call_ids),
                    duplicate_tool_call_ids,
                )

            url = f"{self.litellm_base_url}/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.litellm_api_key}",
                "Content-Type": "application/json",
            }

            data = {
                "model": model,
                "messages": messages,
                "drop_params": self.litellm_drop_params,
                "stream": bool(stream),
            }

            if tools:
                data["tools"] = tools
                data["tool_choice"] = "auto"

            token_cap = (
                max_completion_tokens
                if max_completion_tokens is not None
                else self.litellm_max_completion_tokens
            )
            if token_cap and token_cap > 0:
                data["max_tokens"] = token_cap

            logger.info(
                "Calling LiteLLM at %s with model %s (stream=%s)",
                url,
                model,
                bool(stream),
            )
            logger.debug("Request data: %s", json.dumps(data, indent=2))

            timeout = lite_llm_request_timeout_in_sec or self.litellm_request_timeout_in_sec
            async with httpx.AsyncClient() as client:
                if not stream:
                    response = await client.post(
                        url, headers=headers, json=data, timeout=timeout
                    )
                    if response.status_code != 200:
                        error = _llm_upstream_error(response.status_code)
                        logger.error(
                            "LiteLLM request failed: status=%s code=%s body_bytes=%s",
                            response.status_code,
                            error.code,
                            response.headers.get("content-length", "unknown"),
                        )
                        raise error
                    result = response.json()
                    logger.info("✅ LiteLLM request successful")
                    return result

                async with client.stream(
                    "POST", url, headers=headers, json=data, timeout=timeout
                ) as response:
                    if response.status_code != 200:
                        error = _llm_upstream_error(response.status_code)
                        logger.error(
                            "LiteLLM stream failed: status=%s code=%s body_bytes=%s",
                            response.status_code,
                            error.code,
                            response.headers.get("content-length", "unknown"),
                        )
                        raise error
                    return await self._consume_chat_stream(response, on_content_delta)

        except LLMUpstreamError:
            raise
        except Exception as exc:
            raise _safe_litellm_exception(exc) from exc

    @staticmethod
    def _starts_new_tool_call(current: Optional[dict], fragment: dict) -> bool:
        """Decide whether a streamed tool-call ``fragment`` begins a NEW call.

        We cannot key accumulation on ``index`` alone: providers disagree on how
        multiple tool calls are streamed.

        - OpenAI streams: ``index`` is present on every fragment (reliable) but the
          ``id`` and ``name`` appear only on the FIRST fragment of a call; later
          fragments carry ``arguments`` only.
        - Ollama (via LiteLLM ``ollama_chat``) streams: every fragment is stamped
          ``index=0`` (useless), but each call gets a distinct ``id`` and its full
          ``name`` + ``arguments`` in a single fragment.

        The one boundary signal reliable across both is the *start* markers, which
        never appear on an argument-continuation fragment:

        - a non-empty ``function.name`` while the current call already has a name, or
        - a distinct non-empty ``id`` vs the current call.

        ``id``/``index`` are only used to populate the call's identity, never to
        group fragments.
        """
        if current is None:
            return True
        fn = fragment.get("function") or {}
        name = fn.get("name") or ""
        if name and current["function"]["name"]:
            return True
        frag_id = fragment.get("id") or ""
        if frag_id and current["id"] and frag_id != current["id"]:
            return True
        return False

    async def _consume_chat_stream(self, response, on_content_delta=None) -> dict:
        """Accumulate OpenAI-compatible SSE chat chunks into one completion."""
        content_parts: List[str] = []
        tool_calls: List[dict] = []
        finish_reason = None
        saw_done = False
        usage: dict = {}
        role = "assistant"

        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if chunk.get("usage"):
                usage = chunk["usage"] or usage

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("role"):
                role = delta["role"]
            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                if on_content_delta is not None:
                    await on_content_delta(piece)
            for tc in delta.get("tool_calls") or []:
                current = tool_calls[-1] if tool_calls else None
                if self._starts_new_tool_call(current, tc):
                    current = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                    tool_calls.append(current)
                if tc.get("id"):
                    current["id"] = tc["id"]
                if tc.get("type"):
                    current["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    current["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    current["function"]["arguments"] += fn["arguments"]

        if finish_reason is None and not saw_done:
            raise LLMUpstreamError(
                "UNAVAILABLE",
                "Model stream ended without a completion reason",
            )
        if finish_reason is None:
            finish_reason = "stop"

        message: dict = {"role": role, "content": "".join(content_parts) or None}
        if tool_calls:
            message["tool_calls"] = tool_calls

        logger.info(
            "✅ LiteLLM stream complete (content=%d chars, tool_calls=%d)",
            len(content_parts and "".join(content_parts) or ""),
            len(tool_calls),
        )
        return {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }

    async def process_request(
        self,
        request: str,
        model: str = "gpt-3.5-turbo",
        max_tool_calls: int = None,
        requested_tools: List[str] = None,
        lite_llm_request_timeout_in_sec: int = None
    ) -> dict:
        """Deprecated. Calls should go through ToolExecutionHub.process_request."""
        raise NotImplementedError("ToolExecutionHub handles request processing")

