"""Exercise actual stdio discovery, exact packets, higher bounds and errors."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from code_graph_find_paths import GraphRuntime, get_tool_schema

ROOT = Path(__file__).resolve().parents[1]


async def main():
    with tempfile.TemporaryDirectory() as temp:
        graph = Path(temp)/'chain.json'
        graph.write_text(json.dumps({'nodes':[{'id':str(i)} for i in range(17)],
                                    'edges':[{'source':str(i),'target':str(i+1)} for i in range(16)]}))
        # Set this to an installed console entry point to verify a wheel independently of the checkout.
        executable = os.environ.get('ALL_PATHS_MCP_EXECUTABLE')
        args = ['--graph',str(graph),'serve']
        params = StdioServerParameters(command=executable or sys.executable,
            args=args if executable else ['-m','code_graph_find_paths.cli']+args,
            cwd=temp if executable else str(ROOT))
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                schema = get_tool_schema()
                assert len(tools.tools)==1
                assert tools.tools[0].name==schema['name']
                assert tools.tools[0].description==schema['description']
                assert tools.tools[0].inputSchema==schema['parameters']
                args={'source_nodes':['0'],'target_nodes':['16'],'max_hops':16}
                result=await client.call_tool('graph_find_paths',args)
                assert not result.isError and len(result.content)==1
                with GraphRuntime(graph) as runtime:
                    expected=runtime.query(**args)
                assert result.content[0].text==json.dumps(expected)
                assert expected['pairs'][0]['returned_count']==1 and expected['complete']
                for name,bad in [('graph_find_paths',dict(args,max_hops=True)),
                                 ('graph_find_paths',dict(args,extra=1)),('unknown',{})]:
                    error=await client.call_tool(name,bad)
                    assert error.isError
                    packet=json.loads(error.content[0].text)
                    assert packet['scope']=='transport' and 'pairs' not in packet
    print(json.dumps({'mcp_verified':True,'exact_native_schema':True,'native_packet_unchanged':True,
                      'sixteen_hop_path':True,'validation_errors_are_transport_errors':True}))


if __name__=='__main__':
    asyncio.run(main())
