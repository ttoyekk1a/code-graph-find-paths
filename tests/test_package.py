import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from code_graph_find_paths import GraphRuntime, decode_paths, get_tool_schema, make_graph_find_paths

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / 'example_graph.json'
SHA = hashlib.sha256(GRAPH.read_bytes()).hexdigest()


class PackageTests(unittest.TestCase):
    def test_native_packet_preserved_and_bound_changes(self):
        native = make_graph_find_paths(GRAPH, SHA)
        with GraphRuntime(GRAPH) as graph:
            packet = graph.query(['a'], ['t'])
            self.assertEqual(packet, native(['a'], ['t'], 4))
            self.assertEqual(decode_paths(packet), [[['a', 'b', 't'], ['a', 'c', 'd', 't']]])
            self.assertEqual(decode_paths(graph.query(['a'], ['t'], 2)), [[['a', 'b', 't']]])
            self.assertTrue(packet['complete'])
        with self.assertRaisesRegex(RuntimeError, 'SessionClosed'):
            graph.query(['a'], ['t'])

    def test_hash_and_validation_errors_are_preserved(self):
        with self.assertRaisesRegex(ValueError, 'SHA256Mismatch'):
            GraphRuntime(GRAPH, '0' * 64)
        with GraphRuntime(GRAPH) as graph:
            for bound in [True, 0, 17, 4.0]:
                with self.assertRaises(ValueError):
                    graph.query(['a'], ['t'], bound)

    def test_native_schema_and_budget_overrides(self):
        self.assertEqual(get_tool_schema(), json.loads((ROOT/'tool_schema.json').read_text()))
        with GraphRuntime(GRAPH, max_paths=1) as graph:
            packet = graph.query(['a'], ['t'])
            self.assertIn('path_limit', packet['truncation'])
            self.assertFalse(packet['complete'])
            self.assertEqual(packet['pairs'][0]['returned_count'], 1)
            self.assertEqual(packet, graph.query(['a'], ['t']))

    def test_cli_json_decode_and_errors(self):
        command = [sys.executable, '-m', 'code_graph_find_paths.cli', '--graph', str(GRAPH),
                   'query', '--source', 'a', '--target', 't']
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
        packet = json.loads(result.stdout)
        self.assertEqual(result.stdout, json.dumps(packet)+'\n')
        decoded = subprocess.run(command+['--decode'], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(decoded.stdout), decode_paths(packet))
        invalid = subprocess.run(command+['--max-hops', '17'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(invalid.stdout, '')
        self.assertIn('InvalidHops', invalid.stderr)


if __name__ == '__main__':
    unittest.main()
