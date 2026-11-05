"""MCP transport for the exact native name, parameters and all-path packet."""
import json
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def serve(runtime):
    server = Server('you-only-search-once-in-code-graph', version='2.0.0')
    schema = runtime.schema

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=schema['name'], description=schema['description'],
                           inputSchema=schema['parameters'])]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            if name != schema['name']:
                raise ValueError('UnknownTool: ' + name)
            if type(arguments) is not dict:
                raise ValueError('Arguments must be an object')
            packet = runtime.query(**arguments)
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(packet))])
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            # Native Python calls raise; MCP carries those failures as transport errors.
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type='text', text=json.dumps({'error_type': type(exc).__name__, 'message': str(exc), 'scope': 'transport'}))])

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
