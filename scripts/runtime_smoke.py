"""Real official-SDK local subprocess tool smoke; no model or Docker evaluation."""
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sys
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'graph-find-paths'
OUT = ROOT / 'evidence' / 'runtime-smoke'

def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2), encoding='utf-8')

def main():
    from swegemma.context import SwegemmaContext
    from swegemma.sandbox import SubprocessManager
    from adk_submission.registry import ToolRegistry
    import swegemma.tools.base as base

    OUT.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('frozen_binding', PACKAGE / 'scripts/build_tool.py')
    binding = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binding)
    from decision_lab.graph_native_v1.source_read import live_identity
    graph = ROOT / 'data/graphs/rich_1fc7cb210ae52eb099df39818ddb9569e5b14c7b.json'
    graph_sha = '4425313b2231691fc69c8b1a88e05684437348631e3198608748a971f429b195'
    assert hashlib.sha256(graph.read_bytes()).hexdigest() == graph_sha
    manager = SubprocessManager(base_dir=OUT / 'workspaces', system_site_packages=False)
    sid = manager.start()
    paths = manager.sandboxes[sid]
    session = None
    report = {'runtime': 'official swegemma SubprocessManager and SwegemmaContext',
              'model_requests': 0, 'docker_verified': False, 'swe_repair_verified': False,
              'source_snapshot_loaded': False, 'graph_source_alignment_verified': False,
              'sdk_base_sha256': hashlib.sha256(Path(base.__file__).read_bytes()).hexdigest()}
    try:
        ctx = SwegemmaContext(docker_manager=manager, container_id=sid,
                              task_id='rich_4075', repo='Textualize/rich', max_tool_calls=2)
        probe = ctx.exec("python3 -I -B -c 'import os; print(os.getcwd())'")
        assert probe.exit_code == 0, probe.stderr
        assert Path(probe.stdout.strip()).resolve() == paths['workspace'].resolve()
        assert ctx.sandbox.sandbox_id == sid and paths['root'].is_dir()
        ownership = {'identity': live_identity(ctx, '/workspace'), 'ownership': {
            'attested_owned': True, 'owner': f'runtime_smoke.py:pid={os.getpid()}',
            'record_id': str(uuid.uuid4()), 'task_id': ctx.task_id,
            'container_id': sid, 'workspace_root': '/workspace'},
            'evidence': {'created_by_this_process': True, 'actual_root': str(paths['root']),
                         'actual_workspace': str(paths['workspace']), 'exec_probe': probe.stdout.strip(),
                         'created_at': datetime.now(timezone.utc).isoformat()}}
        write('ownership.json', ownership)
        fn, session = binding.build_graph_find_paths(ctx, graph_path=graph,
                         expected_graph_sha256=graph_sha, runtime_identity=ownership)
        registry = ToolRegistry()
        for name, tool in ctx.create_tools().items():
            registry.register(name, tool)
        registry.register('graph_find_paths', fn)
        assert fn.__name__ == 'graph_find_paths'
        assert list(inspect.signature(fn).parameters) == ['source_nodes','target_nodes','max_hops']
        assert inspect.signature(fn).parameters['max_hops'].default == 4
        assert inspect.getdoc(fn) == binding.TOOL_SCHEMA['function']['description']
        assert 'graph_compare_locations' not in registry
        # Foreign/OpenAI-compatible controller passes the frozen schema unchanged;
        # this is a transport envelope capture, not a request to an LLM service.
        envelope = {'tools': [binding.TOOL_SCHEMA], 'transport': 'local-dispatch-smoke-no-model'}
        write('request-envelope.json', envelope)
        assert json.loads((OUT/'request-envelope.json').read_text())['tools'][0] == json.loads((PACKAGE/'references/tool-schema.json').read_text())
        args = {'source_nodes':['rich.progress.SpinnerColumn.render'],
                'target_nodes':['rich.progress.DownloadColumn.render'], 'max_hops':4}
        attempts=[]
        def dispatch(name, arguments):
            attempts.append({'name':name,'arguments':arguments})
            result=registry.get(name)(**arguments)
            delivered=json.dumps(result)
            assert json.loads(delivered)==result
            return result,delivered
        before=ctx.tool_calls_used
        result,delivered=dispatch('graph_find_paths',args)
        assert ctx.tool_calls_used-before == 1
        assert result['accounting']['core_calls']==1
        assert result['accounting']['native']=={'attempts':1,'admitted':1,'rejected':0}
        assert result['status']=='partial' and result['matrix'][0]['state']=='connected'
        assert len(delivered)<=8000 and len(delivered)==result['accounting']['output_characters']
        assert len(delivered.encode())==result['accounting']['output_bytes']
        rows=json.loads(graph.read_bytes())
        rows=rows.get('edges',rows.get('links'))
        for path in result['paths']:
            assert len(path['steps'])==len(path['node_ids'])-1
            for (a,b),step in zip(zip(path['node_ids'],path['node_ids'][1:]),path['steps']):
                expected=[dict(index=i,source=r['source'],target=r['target'],key=r.get('key'),type=r.get('type')) for i,r in enumerate(rows) if {r['source'],r['target']}=={a,b}]
                assert step['edge_rows']==expected and expected
        (OUT/'delivered.json').write_text(delivered)
        write('raw-result.json',result)
        session.close()
        closed,_=dispatch('graph_find_paths',args)
        assert closed['error_type']=='SessionClosed' and ctx.tool_calls_used==2
        denied,_=dispatch('graph_find_paths',args)
        assert denied['error_type']=='BudgetExceeded' and ctx.tool_calls_used==2
        write('closed-response.json',closed)
        write('budget-response.json',denied)
        write('attempts.json',attempts)
        report.update(schema_exact=True, tool_name_exact=True, registry=registry.list_available(),
                      successful_query_budget_delta=1, successful_query_core_calls=1,
                      complete_delivery=True, original_edge_evidence_verified=True,
                      closed_session_rejected=True, budget_excess_rejected=True,
                      controller_attempts=len(attempts), native=session.native,
                      local_context_verified=True, output_characters=len(delivered))
    finally:
        if session: session.close()
        manager.stop(sid)
        report['cleanup_verified']=sid not in manager.sandboxes and not paths['root'].exists()
        write('report.json',report)
    assert report['cleanup_verified']
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
