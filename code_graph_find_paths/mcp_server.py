"""MCP transport preserving the frozen tool schema and default JSON serialization."""
import contextlib
import json
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def create_server(runtime):
    server = Server('code-graph-find-paths', version='0.2.1')
    declaration = runtime.schema['function']

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=declaration['name'], description=declaration['description'],
                           inputSchema=declaration['parameters'])]

    # Count malformed attempts in our controller, rather than letting the SDK
    # reject them before the controller sees them. Runtime validates all inputs.
    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            with contextlib.redirect_stdout(sys.stderr):
                packet = runtime.dispatch(name, arguments)
        except Exception as exc:
            # Transport errors are outside the frozen native packet contract.
            packet = {'status':'error', 'error_type':type(exc).__name__,
                      'error':str(exc), 'error_scope':'transport'}
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=json.dumps(packet))],
            isError=packet.get('status') == 'error',
        )

    return server


async def serve_stdio(runtime):
    server = create_server(runtime)
    async with stdio_server() as (incoming, outgoing):
        await server.run(incoming, outgoing, server.create_initialization_options())
