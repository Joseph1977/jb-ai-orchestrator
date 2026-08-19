# Multiple MCP Servers Support with Universal Tool Attribution - Implementation Summary

## Overview

The AI Agent service has been enhanced to support connecting to multiple Model Context Protocol (MCP) servers simultaneously with meaningful server names and **universal tool attribution**. This means ALL tools from named servers include server identifiers in their names, providing clear server attribution regardless of naming conflicts. This approach ensures users always know which server provides each tool, improving UX and eliminating confusion.

## Key Changes Made

### 1. Configuration Changes (`src/app/config.py`)

**Before:**
```python
MCP_SERVER_URL = os.getenv('MCP_SERVER_URL', 'http://localhost:8001/mcp')
```

**After:**
- Added `MCP_SERVER_URLS` attribute (list)
- Added `_parse_mcp_servers()` method to support two configuration formats:
  1. **JSON Array with named servers**: `MCP_SERVER_URLS=[{"name": "general", "url": "url1"}, {"name": "test", "url": "url2"}]`
  2. **Numbered URLs with named servers**: `MCP_SERVER_URL_1={"name": "general", "url": "url1"}`, etc.

  Both forms require `{name, url}` objects. A bare URL string, an array of bare
  URL strings, and the pre-multi-server `MCP_SERVER_URL` variable are not
  parsed. See [Configuration Examples](#configuration-examples) for exactly how
  each malformed shape behaves — they do not all fail the same way.

### 2. Enhanced MCPTool Dataclass (`src/app/services/mcp_agent_service.py`)

**Added fields:**
- `server_url`: URL of the MCP server hosting this tool
- `server_name`: Meaningful server identifier (e.g., "general", "test", "custom")
- `original_name`: Original tool name before server attribution is applied

### 3. MCPAgentService Rewrite (`src/app/services/mcp_agent_service.py`)

**New capabilities:**
- **Multiple server connection**: Connects to all configured MCP servers
- **Universal tool attribution**: Suffixes every tool with its server name, so
  collisions cannot arise rather than being resolved after the fact
- **Server routing**: Routes tool execution to the correct server
- **Fault tolerance**: Continues working if individual servers fail
- **Enhanced logging**: Includes server information in all logs

**Key methods:**
- `fetch_mcp_tools_from_server()`: Fetches tools from a specific server
- `fetch_mcp_tools()`: Aggregates tools from all servers and applies attribution
- `find_tool_by_name()`: Locates tools by their attributed names
- `execute_mcp_tool()`: Routes tool execution to the appropriate server

### 4. Enhanced Models (`src/app/models/requests.py`)

**ToolInfo updates:**
- Added `server_url`, `server_id`, `original_name` fields

**ToolCallInfo updates:**
- Added `mcp_server_id`, `mcp_server_url` fields for traceability

### 5. Updated Controller (`src/app/controllers/agent_controller.py`)

- Modified to include server information in tool responses
- Enhanced error handling and logging

### 6. Configuration Examples

**Created new configuration files:**
- `src/.env/localhost/.env.example`: Shows all configuration options
- Updated existing `.env` files with examples

## Tool Naming Algorithm

### Step 1: Collection
- Fetch tools from all configured MCP servers
- Preserve original tool names and server information

### Step 2: Universal Attribution
- **ALL tools** from named servers get server attribution: `{original_name}_{server_name}`
- No conflict detection needed - attribution is universal
- Examples: `search` becomes `search_google`, `weather` becomes `weather_general`

### Step 3: LLM Integration
- Convert resolved tools to LiteLLM format
- Enhance descriptions with meaningful server identifier: `[google] Tool description`
- LLM receives all tools with clear server attribution

### Step 4: Execution Routing
- Map resolved tool names back to original names for server calls
- Route execution to the correct server using server name
- Log server information for traceability with meaningful names

## Configuration Examples

### Named Servers Format

```env
# JSON array — one entry per server
MCP_SERVER_URLS=[{"name": "general", "url": "https://server1.com/mcp"}, {"name": "google", "url": "https://mcp.google.com/mcp"}]
```

```env
# Numbered objects — discovered from _1 upwards until the first gap
MCP_SERVER_URL_1={"name": "general", "url": "https://server1.com/mcp"}
MCP_SERVER_URL_2={"name": "google", "url": "https://mcp.google.com/mcp"}
MCP_SERVER_URL_3={"name": "custom", "url": "http://localhost:8001/mcp"}
```

An entry with a blank or missing name is reported as `default`. Beyond that the
two forms do not validate alike, and the array form is the less forgiving of the
pair:

| Input | Result |
|---|---|
| Numbered entry that is not an object with both keys | Logged as a warning and skipped; discovery continues at the next index |
| Numbered index missing | Discovery stops there, so `_1` plus `_3` yields only `_1` |
| Array entry missing `url` | Retained, then rejected by `validate_config()`: `MCP server at index N must be an object with 'name' and 'url' properties` |
| Array mixing objects and bare strings | Raises `AttributeError: 'str' object has no attribute 'get'` — not a configuration error |
| Array whose first element is not an object | The whole variable is ignored and parsing falls through to the numbered variables |
| Empty array (`[]`) | Ignored the same way — there is no first element to inspect |

`MCP_SERVER_URLS` therefore takes precedence over the numbered variables only
when its first element is an object.

### Unsupported forms

```env
# Not parsed — an array of bare URL strings
MCP_SERVER_URLS=["https://server1.com/mcp", "https://server2.com/mcp"]

# Not parsed — the pre-multi-server single-server variable
MCP_SERVER_URL=https://server1.com/mcp
```

With no numbered variables set, either leaves the server list empty and
`validate_config()` raises `At least one MCP server URL must be configured`
during startup. If numbered variables *are* set, they are used instead and the
unsupported variable is silently ignored.

`tests/test_config_mcp_servers.py` pins every row above.

## Response Format Changes

### Tool Information Response
```json
{
  "success": true,
  "tools": [
    {
      "name": "google_search_general",
      "description": "[general] Search Google for information",
      "input_schema": {...},
      "server_url": "https://server1.com/mcp",
      "server_id": "general",
      "original_name": "google_search"
    },
    {
      "name": "search_google",
      "description": "[google] Search the web",
      "input_schema": {...},
      "server_url": "https://mcp.google.com/mcp",
      "server_id": "google",
      "original_name": "search"
    }
  ]
}
```

### Execute Request Response
```json
{
  "success": true,
  "response": "AI response",
  "tool_calls_info": [
    {
      "tool_index": 1,
      "tool_name": "google_search_general",
      "llm_tool_interaction_index": 1,
      "mcp_server_id": "general",
      "mcp_server_url": "https://server1.com/mcp"
    }
  ]
}
```

## Benefits

1. **Universal Clarity**: ALL tools include server attribution, eliminating any confusion about tool sources
2. **Scalability**: Connect to multiple MCP servers for broader tool coverage
3. **Consistent UX**: Every tool follows the same `{tool}_{server}` naming pattern
4. **Fault Tolerance**: Service continues working if individual servers fail
5. **Enhanced Traceability**: Full logging of which server each tool comes from with meaningful names
6. **No Ambiguity**: Users never need to guess which server provides a tool

## Use Cases

1. **Development/Production**: Different MCP servers for different environments
2. **Specialized Tools**: Each server provides domain-specific tools
3. **Redundancy**: Multiple servers providing similar functionality
4. **A/B Testing**: Compare different implementations of the same tool
5. **Vendor Diversity**: Use tools from different MCP providers simultaneously

## Testing

- `tests/test_config_mcp_servers.py` covers both supported configuration forms,
  blank-name defaulting, numbered discovery and its gap behaviour, malformed
  entries, precedence, and the unsupported forms above.
- `scripts/smoke_service.py` exercises the agent endpoints against a running
  service and prints the server attribution carried on each tool call.

## Logging Enhancements

- Startup logs show all configured MCP servers
- Tool execution logs include server ID and URL
- Server attribution is recorded for every tool, so no conflict resolution is logged
- Server connection failures are logged but don't stop the service
