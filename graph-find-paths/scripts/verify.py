"""Offline contract tests; stdlib-only test context, not a live SDK/Docker gate."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod);return mod

def offline_base():
    # Test-only minimal module containers load the exact frozen budget decorator.
    # They never emulate a real sandbox or prove actual harness integration.
    if any(k=='swegemma' or k.startswith('swegemma.') for k in sys.modules):
        raise RuntimeError('Run verify.py in a fresh isolated Python process')
    for name in ('swegemma','swegemma.tools'):
        mod=types.ModuleType(name);mod.__path__=[];sys.modules[name]=mod
    load('swegemma.tools.base',ROOT/'frozen/vendor/swegemma/tools/base.py')

def stable(packet):
    # Exhaustive allowlist: do not erase statuses, omissions, counts or witnesses.
    if isinstance(packet,list): return [stable(v) for v in packet]
    if isinstance(packet,dict): return {k:stable(v) for k,v in packet.items()
        if k not in ('elapsed_seconds','output_characters','output_bytes')}
    return packet

def verify_packet(p,graph):
    rows=graph.get('edges',graph.get('links'))
    paths={x['path_id']:x for x in p['paths']}
    for path in paths.values():
        ids=path['node_ids'];assert len(ids)==len(set(ids)) and 1<=len(ids)-1<=4
        assert len(path['steps'])==len(ids)-1
        for (a,b),step in zip(zip(ids,ids[1:]),path['steps']):
            assert (step['from'],step['to'])==(a,b)
            expected=[dict(index=i,source=r['source'],target=r['target'],key=r.get('key'),type=r.get('type'))
                for i,r in enumerate(rows) if {r['source'],r['target']}=={a,b}]
            assert step['edge_rows']==expected and expected
    for cell in p['matrix']:
        if cell['state']=='connected': assert cell['path_ids'] and all(x in paths for x in cell['path_ids'])
    assert len(json.dumps(p))<=8000
    assert p['accounting']['output_characters']==len(json.dumps(p))
    assert p['accounting']['output_bytes']==len(json.dumps(p).encode('utf-8'))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--graphs-dir',type=Path)
    args=parser.parse_args()
    manifest=ROOT/'MANIFEST.json'
    if not manifest.is_file():
        raise RuntimeError('MissingPackageManifest')
    for rel,sha in json.loads(manifest.read_text())['files'].items():
        assert hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()==sha, 'PackageHashMismatch:'+rel
    offline_base()
    binding=load('portable_binding',ROOT/'scripts/build_tool.py')
    tests=load('handoff_tests',ROOT/'tests/test_binding.py')
    core=load('frozen_core_tests',ROOT/'frozen/tests/test_graph_native_core_v1.py')
    search=load('frozen_search_tests',ROOT/'frozen/evidence/path-study-expanded-20261103/test_search.py')
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(m) for m in [tests,core,search])
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful(): raise SystemExit(1)
    report=dict(mode='offline frozen-code and synthetic-context contract check',tests=result.testsRun,
        graph_replays=0,real_harness_verified=False,model_requests=0)
    if args.graphs_dir:
        fixtures=json.loads((ROOT/'fixtures/real-graph-replays.json').read_text())
        for row in fixtures:
            matches=list(args.graphs_dir.rglob(row['graph_filename']))
            if len(matches)!=1: raise RuntimeError('Expected exactly one graph file: '+row['graph_filename'])
            graph=matches[0]
            fn,session,ctx=tests.build(graph,row['graph_sha256'])
            try:
                actual=fn(**row['arguments'])
                if stable(actual)!=stable(row['expected']):
                    raise AssertionError('HistoricalPacketMismatch:'+row['query_id'])
                verify_packet(actual,json.loads(graph.read_bytes()))
                assert ctx.tool_calls_used==1 and session.native['admitted']==1
                report['graph_replays']+=1
            finally: session.close()
        report['connected']=sum(r['expected']['matrix'][0]['state']=='connected' for r in fixtures)
        report['unknown']=len(fixtures)-report['connected']
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
