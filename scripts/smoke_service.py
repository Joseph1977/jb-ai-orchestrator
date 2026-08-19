"""
HTTP smoke check for the agent endpoints. Requires a running orchestrator service.

Environment:
  PY_MAIN_AGENT_BASE_URL   service base URL (default http://localhost:8000)
  PY_MAIN_AGENT_SMOKE_MODEL  model id to send (default gpt-4o)
  PY_MAIN_AGENT_SMOKE_TOOLS  comma-separated tool names for the tool-selection
                             check; when unset, names are taken from /getTools
"""

import asyncio
import os

import httpx
import json

BASE_URL = os.getenv("PY_MAIN_AGENT_BASE_URL", "http://localhost:8000")
MODEL = os.getenv("PY_MAIN_AGENT_SMOKE_MODEL", "gpt-4o")
REQUESTED_TOOLS = [
    name.strip()
    for name in os.getenv("PY_MAIN_AGENT_SMOKE_TOOLS", "").split(",")
    if name.strip()
]

async def check_health():
    """Fail unless the service reports healthy."""
    print("Testing health endpoint...")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/isalive")
        print(f"Health check: {response.status_code} - {response.text}")
        if response.status_code != 200:
            raise RuntimeError(f"/isalive returned {response.status_code}")

async def check_get_tools():
    """Fail unless the tool list is retrievable; return the discovered names."""
    print("\nTesting get tools endpoint...")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/v1/agent/getTools")
        if response.status_code != 200:
            print(f"Error: {response.status_code} - {response.text}")
            raise RuntimeError(f"/getTools returned {response.status_code}")

        tools = response.json()
        print(f"Tools response: {json.dumps(tools, indent=2)}")
        if not tools.get("success"):
            raise RuntimeError(f"/getTools reported success=false: {tools.get('error')}")

        discovered = tools.get("tools") or []
        print(f"\nFound {len(discovered)} tools:")
        for tool in discovered:
            server_info = f" (from {tool.get('server_id', 'unknown')})" if tool.get('server_id') else ""
            print(f"  - {tool['name']}{server_info}: {tool['description']}")
        return [tool["name"] for tool in discovered if tool.get("name")]

async def check_execute_request():
    """Test the execute request endpoint"""
    print("\nTesting execute request endpoint...")
    
    # Test basic request without tools
    request_data = {
        "task": "What is the weather like today?",
        "role": "helpful assistant",
        "context": "User is asking about current weather conditions",
        "outputInstruction": "Provide a brief response",
        "model": MODEL
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/v1/agent/executeRequest",
            json=request_data,
            timeout=60.0
        )
        if response.status_code == 200:
            result = response.json()
            print(f"Execute request response: {json.dumps(result, indent=2)}")
            
            # VALIDATE the response properly - fail if there are errors
            if not result.get("success", False):
                print(f"❌ Execute request FAILED: {result.get('error', 'Unknown error')}")
                raise Exception(f"Execute request failed: {result.get('error')}")

            # No tool call is expected here: the task is answerable directly.
            print("✅ Execute request passed")
        else:
            print(f"❌ Execute request FAILED: {response.status_code} - {response.text}")
            raise Exception(f"HTTP error: {response.status_code}")

async def check_tool_selection_request(available_tools):
    """Check that a request naming specific tools is accepted and runs.

    This does not assert that a tool was actually invoked: whether the model
    chooses to call one is not a property of the service, and requiring it is
    what made the previous version of this script fail spuriously.
    """
    tool_names = REQUESTED_TOOLS or available_tools[:2]
    if not tool_names:
        print("\nSkipping tool-selection check: no MCP tools are configured.")
        return

    print(f"\nTesting tool-selection request with: {tool_names}")

    request_data = {
        "task": "Use the available tools to gather something useful, then summarise it.",
        "tools": tool_names,
        "model": MODEL
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/v1/agent/executeRequest",
            json=request_data,
            timeout=60.0
        )
        if response.status_code == 200:
            result = response.json()
            print(f"Tool-selection response: {json.dumps(result, indent=2)}")
            
            # VALIDATE the response properly
            if not result.get("success", False):
                print(f"❌ Tool-selection request FAILED: {result.get('error', 'Unknown error')}")
                raise Exception(f"Tool-selection request failed: {result.get('error')}")
            
            # Show tool call information with server details
            if result.get('tool_calls_info'):
                print(f"\nTool calls made:")
                for call_info in result['tool_calls_info']:
                    server_info = f" on {call_info.get('mcp_server_id', 'unknown')}" if call_info.get('mcp_server_id') else ""
                    print(f"  - {call_info['tool_name']}{server_info}")
            
            print("✅ Tool-selection request passed")
        else:
            print(f"❌ Tool-selection request FAILED: {response.status_code} - {response.text}")
            raise Exception(f"HTTP error: {response.status_code}")

async def main():
    """Run all tests"""
    print("Testing AI Agent MCP Microservice")
    print("=" * 50)
    
    try:
        await check_health()
        available_tools = await check_get_tools()
        await check_execute_request()
        await check_tool_selection_request(available_tools)
        
        print("\n✅ ALL TESTS COMPLETED SUCCESSFULLY")
        
    except Exception as e:
        print(f"\n❌ TESTS FAILED: {str(e)}")
        raise e  # Re-raise to ensure the script exits with error code

if __name__ == "__main__":
    import sys
    try:
        asyncio.run(main())
        sys.exit(0)
    except Exception as e:
        print(f"\n💥 Test suite failed: {e}")
        sys.exit(1) 
