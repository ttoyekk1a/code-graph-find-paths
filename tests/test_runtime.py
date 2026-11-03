"""Integration tests using the real installed SDK and owned subprocess workspaces."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from code_graph_find_paths.runtime import CompetitionRuntime as GraphRuntime, find_bundle, get_tool_schema


@unittest.skipUnless(__import__('importlib.util', fromlist=['find_spec']).find_spec('swegemma'), 'Optional competition SDK is not installed')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.graph = self.parent / 'graph.json'
        self.graph.write_text(json.dumps({'nodes': [{'id': x} for x in ('a', 'b', 'c')],
                                         'edges': [{'source': 'a', 'target': 'b'}, {'source': 'b', 'target': 'c'}]}))
        self.sha = hashlib.sha256(self.graph.read_bytes()).hexdigest()
        self.args = {'source_nodes': ['a'], 'target_nodes': ['c']}

    def runtime(self, **kwargs):
        return GraphRuntime(self.graph, self.sha, work_dir=self.parent, **kwargs)

    def test_real_registry_full_delivery_budget_and_all_attempts(self):
        with self.runtime(tool_calls=4) as runtime:
            self.assertEqual(runtime.context.task_id, 'local-graph')
            self.assertEqual(runtime.schema, get_tool_schema())
            changed = runtime.schema
            changed['function']['name'] = 'changed'
            self.assertEqual(runtime.schema['function']['name'], 'graph_find_paths')
            unknown = runtime.dispatch('missing_tool', {})
            self.assertEqual(unknown, {'status': 'error', 'error_type': 'UnknownTool', 'scope': 'controller'})
            malformed = runtime.query(unknown_argument=True)
            self.assertEqual(malformed, {'status': 'error', 'error_type': 'InvalidArguments', 'scope': 'controller'})
            self.assertEqual(runtime.attempts, 2)
            self.assertEqual(runtime.context.tool_calls_used, 0)
            packet = runtime.query(**self.args)
            self.assertEqual(packet['matrix'][0]['state'], 'connected')
            self.assertEqual(packet['paths'][0]['node_ids'], ['a', 'b', 'c'])
            self.assertEqual(packet['accounting']['core_calls'], 1)
            self.assertEqual(len(json.dumps(packet)), packet['accounting']['output_characters'])
            self.assertEqual(runtime.context.tool_calls_used, 1)
            invalid = runtime.query(**self.args, max_hops=True)
            self.assertEqual(invalid['error_type'], 'InvalidArguments')
            self.assertEqual(runtime.context.tool_calls_used, 2)
            denied = runtime.query(**self.args)
            self.assertEqual(denied['error_type'], 'ControllerAttemptBudgetExceeded')
            self.assertEqual(denied['scope'], 'controller')
            self.assertEqual(runtime.attempts, 5)
            self.assertEqual(runtime.context.tool_calls_used, 2)
            workspace = runtime.workspace_path
            self.assertTrue(workspace.is_dir())
        self.assertFalse(workspace.exists())
        self.assertTrue(runtime.closed)
        runtime.close()
        with self.assertRaises(RuntimeError):
            runtime.start()

    def test_body_exception_cleans_owned_workspace(self):
        runtime = self.runtime()
        with self.assertRaisesRegex(RuntimeError, 'body failure'):
            with runtime:
                workspace = runtime.workspace_path
                raise RuntimeError('body failure')
        self.assertTrue(runtime.closed)
        self.assertFalse(workspace.exists())

    def test_builder_failure_cleans_real_workspace(self):
        def fail(*args, **kwargs):
            raise RuntimeError('injected builder failure')
        runtime = self.runtime()
        # Real SDK context/start/exec still run; only the failing builder is injected.
        with patch('code_graph_find_paths.runtime._load_binding', return_value=SimpleNamespace(build_graph_find_paths=fail)):
            with self.assertRaisesRegex(RuntimeError, 'injected builder failure'):
                runtime.start()
        self.assertTrue(runtime.closed)
        self.assertFalse(runtime.workspace_path.exists())
        self.assertEqual(list(self.parent.iterdir()), [self.graph])

    def test_hash_mismatch_fails_before_creating_workspace(self):
        runtime = GraphRuntime(self.graph, '0' * 64, work_dir=self.parent)
        with self.assertRaisesRegex(ValueError, 'SHA256Mismatch'):
            runtime.start()
        self.assertTrue(runtime.closed)
        self.assertIsNone(runtime.workspace_path)
        self.assertEqual(list(self.parent.iterdir()), [self.graph])

    def test_explicit_bad_bundle_does_not_fall_back(self):
        with patch.dict('os.environ', {'CODE_GRAPH_BUNDLE': str(self.parent / 'absent')}):
            with self.assertRaises(FileNotFoundError):
                find_bundle()


if __name__ == '__main__':
    unittest.main()
