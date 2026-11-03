import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
import hashlib
import threading
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('portable_binding', ROOT/'scripts/build_tool.py')
binding = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(binding)

class Context:
    """Explicit budget/identity test double; not a live Docker attestation."""
    def __init__(self):
        self.task_id='test'; self.container_id='test-only'
        self.sandbox=SimpleNamespace(sandbox_id='test-only', work_dir='/workspace')
        self.tool_lock=threading.RLock(); self.tool_calls_used=0
        self.budget=SimpleNamespace(tool_calls=8)
    def check_budget(self, *, count_tool_call=True):
        if self.tool_calls_used >= self.budget.tool_calls:
            return json.dumps(dict(status='error',error_type='BudgetExceeded'))
        self.tool_calls_used += int(count_tool_call)
        return None

def build(graph, sha):
    ctx=Context()
    from decision_lab.graph_native_v1.source_read import live_identity
    receipt=dict(identity=live_identity(ctx,'/workspace'), ownership=dict(attested_owned=True,
        owner='offline-test-only',record_id='test-only',task_id=ctx.task_id,
        container_id=ctx.container_id,workspace_root='/workspace'))
    fn, session=binding.build_graph_find_paths(ctx,graph_path=graph,expected_graph_sha256=sha,runtime_identity=receipt)
    return fn,session,ctx

class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.graph=Path(self.tmp.name)/'g.json'
        self.graph.write_text(json.dumps({'nodes':[{'id':s} for s in ['g.a','g.b','g.z','other.a']],
            'edges':[{'source':'g.a','target':'g.b'},{'source':'g.b','target':'g.z'}]}))
        self.sha=hashlib.sha256(self.graph.read_bytes()).hexdigest()
        self.fn,self.session,self.ctx=build(self.graph,self.sha); self.addCleanup(self.session.close)
    def test_exact_name_signature_and_schema(self):
        self.assertEqual(self.fn.__name__,'graph_find_paths')
        self.assertEqual(list(inspect.signature(self.fn).parameters),['source_nodes','target_nodes','max_hops'])
        self.assertEqual(inspect.signature(self.fn).parameters['max_hops'].default,4)
        self.assertEqual(inspect.getdoc(self.fn),binding.TOOL_SCHEMA['function']['description'])
        self.assertEqual(self.session.core.mode,'bidirectional')
    def test_positive_partial_and_paid_work(self):
        p=self.fn(['g.a'],['g.z'])
        self.assertEqual(p['status'],'partial'); self.assertEqual(p['mode'],'C')
        self.assertEqual(p['matrix'][0]['state'],'connected')
        self.assertEqual(p['paths'][0]['node_ids'],['g.a','g.b','g.z'])
        self.assertEqual(self.ctx.tool_calls_used,1);self.assertEqual(p['accounting']['core_calls'],1)
    def test_identity_ambiguity_unknown_and_invalid_consumes(self):
        for left,right in [('g.a','g.a'),('a','g.z')]:
            p=self.fn([left],[right]);self.assertEqual(p['matrix'][0]['state'],'unknown')
        p=self.fn(['g.a'],['g.z'],True)
        self.assertEqual(p['error_type'],'InvalidArguments');self.assertEqual(self.ctx.tool_calls_used,3)
    def test_wrong_hash_rejected(self):
        with self.assertRaisesRegex(ValueError,'SHA256Mismatch'): build(self.graph,'0'*64)
    def test_closed_and_changed_runtime_rejected(self):
        self.ctx.task_id='changed'
        self.assertEqual(self.fn(['g.a'],['g.z'])['error_type'],'RuntimeChanged')
        self.session.close()
        self.assertEqual(self.fn(['g.a'],['g.z'])['error_type'],'SessionClosed')

if __name__=='__main__': unittest.main()
