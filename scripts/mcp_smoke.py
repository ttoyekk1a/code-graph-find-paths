"""Real MCP initialize/list/call smoke. No model requests or evaluator prompts."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / 'data/graphs/rich_1fc7cb210ae52eb099df39818ddb9569e5b14c7b.json'
SHA = '4425313b2231691fc69c8b1a88e05684437348631e3198608748a971f429b195'
ARGS = {'source_nodes':['rich.progress.SpinnerColumn.render'],
        'target_nodes':['rich.progress.DownloadColumn.render'], 'max_hops':4}

async def main():
    expected = json.loads((ROOT/'graph-find-paths/references/tool-schema.json').read_text())['function']
    temp = tempfile.TemporaryDirectory(prefix='mcp_cleanup_check_')
    server = StdioServerParameters(command=sys.executable,
        args=['-m','code_graph_find_paths.cli','--graph',str(GRAPH),'--sha256',SHA,
              '--backend','competition','--tool-calls','3','serve','stdio'], cwd=ROOT, env={'TMPDIR':temp.name})
    packets = []
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            initialized = await client.initialize()
            discovered = await client.list_tools()
            assert len(discovered.tools) == 1
            tool = discovered.tools[0]
            assert tool.name == expected['name']
            assert tool.description == expected['description']
            assert tool.inputSchema == expected['parameters']
            for name, args in [('graph_find_paths',ARGS), ('unknown_tool',{}),
                               ('graph_find_paths',{'unexpected':True}), ('graph_find_paths',ARGS)]:
                answer = await client.call_tool(name,args)
                assert len(answer.content) == 1 and answer.content[0].type == 'text'
                text = answer.content[0].text
                packet = json.loads(text)
                assert text == json.dumps(packet), 'Serialization changed'
                packets.append(packet)
            assert packets[0]['matrix'][0]['state']=='connected'
            assert packets[0]['mode']=='C' and packets[0]['paths']
            assert packets[0]['accounting']['output_characters']==len(json.dumps(packets[0]))
            assert all(p['status']=='error' for p in packets[1:])
            assert 'Budget' in packets[3].get('error_type',''), packets[3]
    assert not list(Path(temp.name).glob('code_graph_runtime_*')), 'Runtime workspace leaked on stdio close'
    temp.cleanup()
    report = {'stdio_cleanup_verified':True, 'protocol': initialized.protocolVersion, 'tool_names':[tool.name],
              'exact_schema':True, 'default_json_serialization':True,
              'connected_query':True, 'unknown_and_invalid_consume_attempts':True,
              'fourth_attempt_rejected':True, 'model_requests':0,
              'packets':packets}
    (ROOT/'evidence/mcp-smoke.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='packets'},indent=2))

if __name__=='__main__':
    asyncio.run(main())
