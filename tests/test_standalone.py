"""Standalone public API/CLI contracts, using only small user-owned JSON graphs."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from code_graph_find_paths.runtime import GraphRuntime, get_tool_schema

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/code-graph.json'
ARGS = {'source_nodes': ['app.entry'], 'target_nodes': ['app.store']}


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def write_graph(self, *, rows=None, edge_key='edges'):
        graph = {'nodes': [{'id': name} for name in ['app.entry', 'app.service', 'app.store']],
                 edge_key: rows if rows is not None else [
                     {'source': 'app.entry', 'target': 'app.service', 'type': 'calls'},
                     {'source': 'app.service', 'target': 'app.store', 'type': 'calls'}]}
        path = self.directory / 'graph.json'
        path.write_text(json.dumps(graph), encoding='utf-8')
        return path, graph

    def test_example_without_hash_and_complete_evidence(self):
        original = EXAMPLE.read_bytes()
        with GraphRuntime(EXAMPLE) as runtime:
            packet = runtime.query(**ARGS)
            self.assertEqual(packet['matrix'][0]['state'], 'connected')
            self.assertEqual(packet['paths'][0]['node_ids'], ['app.entry', 'app.service', 'app.store'])
            self.assertEqual(packet['graph_sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(packet['source_grounding'], 'not_performed')
            self.assertEqual(packet['accounting']['output_characters'], len(json.dumps(packet)))
            self.assertLessEqual(len(json.dumps(packet)), 8000)
        self.assertEqual(EXAMPLE.read_bytes(), original)

    def test_long_session_warning_is_graph_specific(self):
        with GraphRuntime(EXAMPLE, tool_calls=20) as runtime:
            for _ in range(10):
                packet = runtime.query(**ARGS)
            self.assertIn('graph queries', packet['budget_warning'])
            self.assertNotIn('submit_patch', json.dumps(packet))
            self.assertEqual(packet['accounting']['common_guard_work'],
                             'standalone_local_session_identity')
            self.assertEqual(packet['accounting']['output_characters'], len(json.dumps(packet)))

    def test_duplicate_and_reciprocal_original_rows_are_retained(self):
        rows = [{'source': 'app.entry', 'target': 'app.service', 'key': 7, 'type': 'calls'},
                {'source': 'app.entry', 'target': 'app.service', 'key': 7, 'type': 'calls'},
                {'source': 'app.service', 'target': 'app.entry', 'key': 9, 'type': 'imports'},
                {'source': 'app.service', 'target': 'app.store'}]
        path, _ = self.write_graph(rows=rows)
        with GraphRuntime(path) as runtime:
            packet = runtime.query(**ARGS)
        evidence = packet['paths'][0]['steps'][0]['edge_rows']
        self.assertEqual(evidence, [dict(index=i, **row) for i, row in enumerate(rows[:3])])
        self.assertEqual(json.loads(path.read_text())['edges'], rows)

    def test_links_graph_is_accepted_without_rewriting(self):
        path, _ = self.write_graph(edge_key='links')
        original = path.read_bytes()
        with GraphRuntime(path) as runtime:
            self.assertEqual(runtime.query(**ARGS)['matrix'][0]['state'], 'connected')
        self.assertEqual(path.read_bytes(), original)

    def test_hash_is_checked_when_supplied(self):
        with self.assertRaisesRegex(ValueError, 'SHA256Mismatch'):
            with GraphRuntime(EXAMPLE, '0' * 64):
                self.fail('Wrong graph hash accepted')
        sha = hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
        with GraphRuntime(EXAMPLE, sha) as runtime:
            self.assertEqual(runtime.query(**ARGS)['graph_sha256'], sha)

    def test_missing_and_malformed_graphs_fail(self):
        with self.assertRaises(FileNotFoundError):
            with GraphRuntime(self.directory / 'missing.json'):
                self.fail('Missing graph accepted')
        path, _ = self.write_graph(rows=[{'source': 'app.entry', 'target': 'not.declared'}])
        with self.assertRaises(ValueError):
            with GraphRuntime(path):
                self.fail('Undeclared graph endpoint accepted')

    def test_unknown_tool_and_bad_arguments_consume_attempt_budget(self):
        with GraphRuntime(EXAMPLE, tool_calls=3) as runtime:
            self.assertEqual(runtime.dispatch('unknown_tool', {})['error_type'], 'UnknownTool')
            self.assertEqual(runtime.query(unexpected=True)['error_type'], 'InvalidArguments')
            self.assertEqual(runtime.query(**ARGS, max_hops=True)['error_type'], 'InvalidArguments')
            self.assertEqual(runtime.query(**ARGS)['error_type'], 'ControllerAttemptBudgetExceeded')
            self.assertEqual(runtime.attempts, 4)

    def test_unknown_node_is_not_a_disconnected_claim(self):
        with GraphRuntime(EXAMPLE) as runtime:
            packet = runtime.query(source_nodes=['missing'], target_nodes=['app.store'])
            self.assertEqual(packet['matrix'][0]['state'], 'unknown')
            self.assertFalse(packet['paths'])

    def test_multi_pair_fallback_preserves_connected_and_identity_unknown(self):
        with GraphRuntime(EXAMPLE) as runtime:
            packet = runtime.query(source_nodes=['app.entry', 'app.service'],
                                   target_nodes=['app.service', 'app.store'])
        cells = {(cell['anchor_index'], cell['candidate_index']): cell for cell in packet['matrix']}
        self.assertEqual(len(cells), 4)
        self.assertEqual(cells[(1, 0)]['state'], 'unknown')
        paths = {path['path_id']: path for path in packet['paths']}
        for pair in [(0, 0), (0, 1), (1, 1)]:
            self.assertEqual(cells[pair]['state'], 'connected')
            self.assertTrue(cells[pair]['path_ids'])
            self.assertTrue(all(pid in paths for pid in cells[pair]['path_ids']))

    def test_schema_is_frozen_and_defensively_copied(self):
        with GraphRuntime(EXAMPLE) as runtime:
            self.assertEqual(runtime.schema, get_tool_schema())
            changed = runtime.schema
            changed['function']['name'] = 'changed'
            self.assertEqual(runtime.schema['function']['name'], 'graph_find_paths')

    def test_close_is_idempotent_and_prevents_reuse(self):
        runtime = GraphRuntime(EXAMPLE)
        with runtime:
            self.assertEqual(runtime.query(**ARGS)['matrix'][0]['state'], 'connected')
        runtime.close()
        self.assertTrue(runtime.closed)
        with self.assertRaises(RuntimeError):
            runtime.query(**ARGS)
        with self.assertRaises(RuntimeError):
            runtime.start()

    def test_sdk_imports_are_not_needed_even_when_blocked(self):
        script = '''import importlib.abc, sys
class BlockCompetitionSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'swegemma', 'adk_submission', 'adk_eval_core'}:
            raise AssertionError('Standalone attempted SDK import: ' + fullname)
sys.meta_path.insert(0, BlockCompetitionSDK())
from code_graph_find_paths.runtime import GraphRuntime
with GraphRuntime(sys.argv[1]) as runtime:
    packet = runtime.query(source_nodes=['app.entry'], target_nodes=['app.store'])
    assert packet['matrix'][0]['state'] == 'connected'
'''
        result = subprocess.run([sys.executable, '-c', script, str(EXAMPLE)], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_default_needs_no_hash_and_prints_one_full_json_packet(self):
        result = subprocess.run([sys.executable, '-m', 'code_graph_find_paths.cli',
                                 '--graph', str(EXAMPLE), 'query', '--source', 'app.entry',
                                 '--target', 'app.store'], cwd=ROOT, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(result.stdout)
        self.assertEqual(packet['matrix'][0]['state'], 'connected')
        self.assertEqual(result.stdout, json.dumps(packet) + '\n')

    def test_competition_backend_requires_explicit_hash(self):
        result = subprocess.run([sys.executable, '-m', 'code_graph_find_paths.cli',
                                 '--backend', 'competition', '--graph', str(EXAMPLE),
                                 'query', '--source', 'app.entry', '--target', 'app.store'],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sha256', result.stderr.lower())
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
