"""Exercise the standalone MCP server using its real stdio client transport."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / 'examples/code-graph.json'
ARGS = {'source_nodes': ['app.entry'], 'target_nodes': ['app.store'], 'max_hops': 4}


async def main():
    expected = json.loads((ROOT / 'graph-find-paths/references/tool-schema.json').read_text())['function']
    packets = []
    with tempfile.TemporaryDirectory(prefix='standalone_mcp_check_') as temporary:
        server = StdioServerParameters(command=sys.executable,
            args=['-m', 'code_graph_find_paths.cli', '--graph', str(GRAPH),
                  '--tool-calls', '3', 'serve', 'stdio'], cwd=ROOT, env={'TMPDIR': temporary})
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                discovered = await client.list_tools()
                assert len(discovered.tools) == 1
                tool = discovered.tools[0]
                assert tool.name == expected['name']
                assert tool.description == expected['description']
                assert tool.inputSchema == expected['parameters']
                for name, arguments in [('graph_find_paths', ARGS), ('unknown_tool', {}),
                                        ('graph_find_paths', {'unexpected': True}),
                                        ('graph_find_paths', ARGS)]:
                    answer = await client.call_tool(name, arguments)
                    assert len(answer.content) == 1 and answer.content[0].type == 'text'
                    text = answer.content[0].text
                    packet = json.loads(text)
                    assert text == json.dumps(packet), 'Default JSON serialization changed'
                    assert answer.isError == (packet['status'] == 'error')
                    packets.append(packet)
                assert packets[0]['matrix'][0]['state'] == 'connected'
                assert packets[0]['paths'][0]['node_ids'] == ['app.entry', 'app.service', 'app.store']
                assert packets[0]['accounting']['output_characters'] == len(json.dumps(packets[0]))
                assert len(json.dumps(packets[0])) <= 8000
                assert [p['error_type'] for p in packets[1:]] == [
                    'UnknownTool', 'InvalidArguments', 'ControllerAttemptBudgetExceeded']
        assert not list(Path(temporary).glob('code_graph_runtime_*')), 'Runtime workspace leaked'
    report = {'backend': 'standalone', 'protocol': initialized.protocolVersion,
              'tool_names': [tool.name], 'exact_schema': True, 'default_json_serialization': True,
              'connected_query': True, 'unknown_and_invalid_consume_attempts': True,
              'fourth_attempt_rejected': True, 'stdio_cleanup_verified': True, 'model_requests': 0}
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
