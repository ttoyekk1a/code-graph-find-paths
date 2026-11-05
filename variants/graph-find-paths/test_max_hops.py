import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest

from tool import AllPathsSession


def decode(packet):
    return {tuple(packet['nodes'][i] for i in path['nodes'])
            for path in packet['pairs'][0].get('paths', [])}


class MaxHopsTests(unittest.TestCase):
    def make(self, edges, **limits):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = Path(temp.name) / 'graph.json'
        nodes = sorted({n for edge in edges for n in edge})
        raw = json.dumps({'nodes': [{'id': n} for n in nodes],
                          'edges': [{'source': a, 'target': b} for a, b in edges]}).encode()
        p.write_bytes(raw)
        return AllPathsSession(p, hashlib.sha256(raw).hexdigest(), **limits)

    def test_long_chains_and_boundaries(self):
        nodes = [f'n{i:02}' for i in range(18)]
        session = self.make(list(zip(nodes, nodes[1:])))
        for hops in (1, 4, 5, 8, 15, 16):
            with self.subTest(hops=hops):
                try:
                    packet = session.graph_find_paths([nodes[0]], [nodes[hops]], hops)
                except ValueError as error:
                    self.fail(f'Supported hop bound {hops} was rejected: {error}')
                self.assertTrue(packet['complete'])
                self.assertEqual(decode(packet), {tuple(nodes[:hops + 1])})
        self.assertEqual(decode(session.graph_find_paths([nodes[0]], [nodes[17]], 16)), set())
        self.assertEqual(decode(session.graph_find_paths([nodes[0]], [nodes[5]])), set())
        self.assertEqual(session.graph_find_paths([nodes[0]], [nodes[4]])['max_hops'], 4)

    def test_multiple_lengths_and_reciprocal_dedup(self):
        branches = [('s', 't')]
        for length in (4, 8, 16):
            branches.append(tuple(['s'] + [f'b{length}_{i}' for i in range(length - 1)] + ['t']))
        edges = [edge for branch in branches for edge in zip(branch, branch[1:])]
        session = self.make(edges + [(b, a) for a, b in edges] + edges[:2] + [('s', 's')])
        for hops in (4, 8, 16):
            packet = session.graph_find_paths(['s'], ['t'], hops)
            expected = {branch for branch in branches if len(branch) - 1 <= hops}
            self.assertTrue(packet['complete'])
            self.assertEqual(decode(packet), expected)
            self.assertEqual(len(packet['pairs'][0]['paths']), len(expected))
            reverse = session.graph_find_paths(['t'], ['s'], hops)
            self.assertEqual(decode(reverse), {tuple(reversed(p)) for p in expected})

    def test_invalid_bound_types(self):
        session = self.make([('s', 't')])
        for hops in (0, 17, -1, True, False, 4.0, '8', None):
            with self.subTest(hops=hops), self.assertRaises(ValueError):
                session.graph_find_paths(['s'], ['t'], hops)

    def test_schema_bound_and_default(self):
        schema = json.loads(Path(__file__).with_name('tool_schema.json').read_text())
        self.assertEqual(schema['name'], 'graph_find_paths')
        self.assertEqual(schema['parameters']['properties']['max_hops'],
                         {'type': 'integer', 'minimum': 1, 'maximum': 16, 'default': 4})

    def test_high_hop_guards_stay_truthful(self):
        edges = [('s', 'a'), ('a', 't'), ('s', 'b'), ('b', 't')]
        for limit, reason in [('max_paths', 'path_limit'), ('max_dfs_states', 'dfs_state_limit'),
                              ('max_edge_rows', 'edge_row_limit')]:
            session = self.make(edges, **{limit: 1})
            packet = session.graph_find_paths(['s'], ['t'], 16)
            self.assertFalse(packet['complete'])
            self.assertFalse(packet['search_complete'])
            self.assertIn(reason, packet['truncation'])
            self.assertNotEqual(packet['pairs'][0]['status'], 'no_path')

    def test_small_graphs_against_breadth_first_path_oracle(self):
        rng = random.Random(20261105)
        nodes = ['s', 'a', 'b', 'c', 'd', 't']
        for _ in range(20):
            edges = [(a, b) for i, a in enumerate(nodes) for b in nodes[i+1:] if rng.random() < .4]
            edges += [(n, n) for n in nodes]
            adjacency = {n: set() for n in nodes}
            for a, b in edges:
                adjacency[a].add(b)
                adjacency[b].add(a)
            session = self.make(edges)
            for bound in (4, 8, 16):
                pending, expected = [('s',)], set()
                while pending:
                    path = pending.pop(0)
                    if path[-1] == 't':
                        expected.add(path)
                    elif len(path) - 1 < bound:
                        pending.extend(path + (n,) for n in adjacency[path[-1]] if n not in path)
                packet = session.graph_find_paths(['s'], ['t'], bound)
                self.assertTrue(packet['complete'])
                self.assertEqual(decode(packet), expected)


if __name__ == '__main__':
    unittest.main()
