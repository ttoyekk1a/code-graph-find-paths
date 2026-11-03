"""Synthetic-only independent graph/witness oracle for experiment search."""
import copy
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import random
import tempfile
import unittest

from decision_lab.graph_native_v1.core import GraphWitnessSession

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('expanded_search', HERE / 'search.py')
SEARCH = importlib.util.module_from_spec(SPEC) if SPEC and SPEC.loader and (HERE / 'search.py').exists() else None
if SEARCH is not None:
    SPEC.loader.exec_module(SEARCH)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SEARCH, 'target-aware implementation is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def core(self, nodes, edges, output_limit=8000):
        graph = {'nodes': [{'id': n} for n in nodes], 'edges': [
            {'source': a, 'target': b, 'key': i, 'type': 'stored'} for i, (a, b) in enumerate(edges)]}
        raw = json.dumps(graph).encode()
        path = Path(self.tmp.name) / 'graph.json'
        path.write_bytes(raw)
        return GraphWitnessSession(path, hashlib.sha256(raw).hexdigest(), output_limit=output_limit), graph

    def oracle_distance(self, graph, source, target):
        adjacency = {n['id']: set() for n in graph['nodes']}
        for row in graph['edges']:
            adjacency[row['source']].add(row['target'])
            adjacency[row['target']].add(row['source'])
        seen, queue = {source}, [(source, 0)]
        for node, distance in queue:
            if node == target:
                return distance
            for nxt in sorted(adjacency[node] - seen):
                seen.add(nxt)
                queue.append((nxt, distance + 1))
        return None

    def validate(self, result, graph, source, target, hops, nodes=160, rows=800):
        self.assertLessEqual(result['work']['visited_nodes'], nodes)
        self.assertLessEqual(result['work']['edge_rows_examined'], rows)
        for path in result.get('paths', []):
            ids = path['node_ids']
            self.assertEqual((ids[0], ids[-1]), (source, target))
            self.assertEqual(len(ids), len(set(ids)))
            self.assertLessEqual(len(ids) - 1, hops)
            self.assertEqual(len(path['steps']), len(ids) - 1)
            for (a, b), step in zip(zip(ids, ids[1:]), path['steps']):
                self.assertEqual((step['from'], step['to']), (a, b))
                expected = [dict(index=i, **row) for i, row in enumerate(graph['edges'])
                            if {row['source'], row['target']} == {a, b}]
                self.assertTrue(expected)
                self.assertEqual(step['edge_rows'], expected)
        for cell in result.get('matrix', []):
            if cell['state'] == 'hop_bound':
                distance = self.oracle_distance(graph, source, target)
                self.assertTrue(distance is None or distance > hops)

    def test_targeted_path_and_fresh_shared_index(self):
        core, graph = self.core(['a', 'b', 'c', 'z'], [('a', 'b'), ('b', 'c'), ('c', 'z')])
        for mode in ('earlystop', 'bidirectional'):
            candidate = SEARCH.TargetedCore.from_loaded(core, mode=mode)
            self.assertIsNot(candidate, core)
            self.assertIs(candidate._adjacency, core._adjacency)
            result = candidate.graph_find_paths(['a'], ['z'], 3)
            self.validate(result, graph, 'a', 'z', 3)
            self.assertTrue(result['paths'])
            self.assertFalse(result['frontier']['complete'])
            self.assertFalse(result['frontier']['by_anchor'][0]['complete'])
            self.assertEqual(result['frontier']['reason'], 'target_witness_found')
            again = candidate.graph_find_paths(['a'], ['z'], 1)
            self.assertFalse(again['paths'])
            self.assertNotEqual(again['matrix'][0]['state'], 'hop_bound')
        self.assertEqual(core.MAX_VISITED_NODES, 160)

    def test_relation_rows_atomic_and_both_roots_count(self):
        core, graph = self.core(['a', 'z'], [('a', 'z'), ('z', 'a'), ('a', 'z')])
        for mode in ('earlystop', 'bidirectional'):
            candidate = SEARCH.TargetedCore.from_loaded(core, mode=mode, max_nodes=2, max_rows=2)
            result = candidate.graph_find_paths(['a'], ['z'])
            self.assertFalse(result['paths'])
            self.assertEqual(result['work']['edge_rows_examined'], 2)
            self.assertEqual(result['work']['canonical_relations_examined'], 1)
            if mode == 'bidirectional':
                self.assertEqual(result['work']['visited_nodes'], 2)
            candidate = SEARCH.TargetedCore.from_loaded(core, mode=mode, max_nodes=2, max_rows=3)
            result = candidate.graph_find_paths(['a'], ['z'])
            self.assertTrue(result['paths'])
            self.validate(result, graph, 'a', 'z', 4, 2, 3)

    def test_small_node_cap_and_existing_other_root(self):
        core, graph = self.core(['a', 'z'], [('a', 'z')])
        candidate = SEARCH.TargetedCore.from_loaded(core, max_nodes=2)
        result = candidate.graph_find_paths(['a'], ['z'])
        self.assertTrue(result['paths'])
        self.validate(result, graph, 'a', 'z', 4, 2)
        candidate = SEARCH.TargetedCore.from_loaded(core, max_nodes=1)
        result = candidate.graph_find_paths(['a'], ['z'])
        self.assertFalse(result['paths'])
        self.assertLessEqual(result['work']['visited_nodes'], 1)

    def test_exhaustive_four_nodes(self):
        names = ['a', 'b', 'c', 'd']
        pairs = list(itertools.combinations(names, 2))
        for mask in range(1 << len(pairs)):
            core, graph = self.core(names, [p for i, p in enumerate(pairs) if mask & (1 << i)])
            for source, target in itertools.permutations(names, 2):
                distance = self.oracle_distance(graph, source, target)
                for mode in ('earlystop', 'bidirectional'):
                    candidate = SEARCH.TargetedCore.from_loaded(core, mode=mode)
                    for hops in range(1, 5):
                        result = candidate.graph_find_paths([source], [target], hops)
                        self.validate(result, graph, source, target, hops)
                        self.assertEqual(bool(result['paths']), distance is not None and distance <= hops)

    def test_seeded_multigraphs_and_caps(self):
        rng = random.Random(20261103)
        names = [f'n{i}' for i in range(9)]
        for _ in range(45):
            edges = [rng.choice(list(itertools.product(names, repeat=2))) for _ in range(rng.randrange(8, 35))]
            core, graph = self.core(names, edges)
            for mode in ('earlystop', 'bidirectional'):
                for max_nodes, max_rows in ((3, 4), (5, 13), (160, 800)):
                    candidate = SEARCH.TargetedCore.from_loaded(core, mode=mode, max_nodes=max_nodes, max_rows=max_rows)
                    for hops in range(1, 5):
                        result = candidate.graph_find_paths(['n0'], ['n8'], hops)
                        self.validate(result, graph, 'n0', 'n8', hops, max_nodes, max_rows)
                        if max_nodes == 160:
                            distance = self.oracle_distance(graph, 'n0', 'n8')
                            self.assertEqual(bool(result['paths']), distance is not None and distance <= hops)

    def test_fallback_validation_and_radius_factory(self):
        core, _ = self.core(['a', 'ns.x', 'other.x', 'z'], [('a', 'z')])
        candidate = SEARCH.TargetedCore.from_loaded(core)
        for sources, targets in ((['a', 'z'], ['z']), (['a'], ['a']), (['missing'], ['z']), (['x'], ['z'])):
            got = candidate.graph_find_paths(sources, targets)
            expected = core.graph_find_paths(sources, targets)
            for result in (got, expected):
                result['work'].pop('elapsed_seconds')
            self.assertEqual(got, expected)
            self.assertEqual(candidate.last_search_audit['algorithm'], 'radius_fallback')
        for args in ((['a'], ['z'], True), ([], ['z'], 4), (['a'], ['z'], 5)):
            with self.assertRaises(ValueError):
                candidate.graph_find_paths(*args)
        self.assertEqual(candidate.graph_compare_locations(['a'], ['z'])['paths'][0]['node_ids'], ['a', 'z'])
        radius = SEARCH.clone_radius(core, max_nodes=40, max_rows=200)
        self.assertIs(type(radius), GraphWitnessSession)
        self.assertEqual((radius.MAX_VISITED_NODES, radius.MAX_EDGE_ROWS), (40, 200))
        self.assertEqual((core.MAX_VISITED_NODES, core.MAX_EDGE_ROWS), (160, 800))

    def test_output_limit_and_long_ids(self):
        a, z = 'a' * 500, 'z' * 500
        core, _ = self.core([a, z], [(a, z)], output_limit=512)
        result = SEARCH.TargetedCore.from_loaded(core).graph_find_paths([a], [z])
        self.assertEqual(result['error'], 'OutputLimitExceeded')
        self.assertLessEqual(len(json.dumps(result)), 512)
        self.assertNotIn('paths', result)

    def test_no_shortest_claim_and_lean_positive_unknown_normalization(self):
        from decision_lab.graph_native_lean_v1.session import LeanGraphWitnessSession
        core, _ = self.core(['a', 'b', 'z', 'isolated'], [('a', 'b'), ('b', 'z')])
        candidate = SEARCH.TargetedCore.from_loaded(core)
        lean = LeanGraphWitnessSession.__new__(LeanGraphWitnessSession)
        lean.graph_sha256, lean.topology_enabled = core.graph_sha256, True
        for target, expected in (('z', 'connected'), ('isolated', 'unknown')):
            result = candidate.graph_find_paths(['a'], [target])
            self.assertFalse(any('shortest' in text.lower() for text in result['limitations']))
            packet = lean._normalize(lean._packet(4), result)
            self.assertEqual(packet['matrix'][0]['state'], expected)
            self.assertFalse(packet['frontier']['complete'])
            if expected == 'connected':
                self.assertEqual(packet['status'], 'partial')
            compact = SEARCH.compact_packet(packet)
            self.assertLessEqual(len(json.dumps(compact)), len(json.dumps(packet)))
            self.assertEqual(compact['paths'], packet['paths'])

    def test_production_node_and_row_boundaries(self):
        nodes = ['a', 'z'] + [f'n{i:03d}' for i in range(160)]
        core, graph = self.core(nodes, [('a', n) for n in nodes[2:]] + [('a', 'z')])
        for mode in ('earlystop', 'bidirectional'):
            candidate = SEARCH.TargetedCore.from_loaded(core, mode=mode)
            result = candidate.graph_find_paths(['a'], ['z'])
            self.assertFalse(result['paths'])
            self.assertEqual(result['work']['visited_nodes'], 160)
            self.validate(result, graph, 'a', 'z', 4)
        for count in (799, 800, 801):
            core, graph = self.core(['a', 'z'], [('a', 'z')] * count)
            core.output_limit = 8000
            for mode in ('earlystop', 'bidirectional'):
                candidate = SEARCH.TargetedCore.from_loaded(core, mode=mode)
                result = candidate.graph_find_paths(['a'], ['z'])
                self.assertEqual(result['work']['edge_rows_examined'], min(count, 800))
                self.assertEqual(candidate.last_search_audit['termination_reason'],
                                 'target_witness_found' if count <= 800 else 'edge_budget_exhausted')
                # Giant genuine receipt must be omitted whole at output cap.
                self.assertFalse(result.get('paths'))
                self.assertLessEqual(len(json.dumps(result)), 8000)

    def test_repeated_row_charging_and_invalid_constructor(self):
        core, graph = self.core(['a', 'b', 'c', 'z'], [('a', 'b'), ('b', 'c')])
        candidate = SEARCH.TargetedCore.from_loaded(core, mode='earlystop')
        result = candidate.graph_find_paths(['a'], ['z'], 3)
        self.assertEqual(result['work']['edge_rows_examined'], 4)
        self.assertEqual(result['work']['canonical_relations_examined'], 2)
        for kwargs in ({'mode': 'guess'}, {'max_nodes': True}, {'max_rows': 0}):
            with self.assertRaises(ValueError):
                SEARCH.TargetedCore.from_loaded(core, **kwargs)

    def test_compact_pure_semantic_projection_and_bounds(self):
        packet = dict(version='v', mode='C', resolution={'sources': ['echo']}, graph_sha256='a' * 64,
                      paths=[{'path_id': 'p0', 'node_ids': ['long.a', 'long.z'], 'steps': [{'from': 'long.a', 'to': 'long.z', 'edge_rows': [{'index': 5, 'source': 'long.z', 'target': 'long.a', 'key': 'k', 'type': 'calls'}]}]}],
                      matrix=[{'state': 'connected', 'path_ids': ['p0']}], frontier={'complete': False},
                      status='partial', reason='bounded_evidence_incomplete', omissions={'paths': 0},
                      requested_bound={'max_hops': 4}, next_action='Use retained paths.', semantics='stored_undirected_adjacency', source_grounding='not_performed',
                      accounting={'graph': {'visited_nodes': 6}, 'output_characters': 2000})
        original = copy.deepcopy(packet)
        result = SEARCH.compact_packet(packet)
        self.assertEqual(packet, original)
        for key in SEARCH.COMPACT_EVIDENCE_KEYS:
            if key in packet:
                self.assertEqual(result[key], packet[key])
        self.assertEqual(result.get('resolution'), packet['resolution'])
        self.assertNotIn('accounting', result)
        self.assertLessEqual(len(json.dumps(result)), len(json.dumps(packet)))
        result['paths'][0]['node_ids'].append('mutation')
        self.assertEqual(packet, original)
        packet.update(status='error', error_type='OutputLimitExceeded', paths=[], matrix=[])
        self.assertEqual(SEARCH.compact_packet(packet)['error_type'], 'OutputLimitExceeded')

    def test_compact_preserves_ambiguous_suffix_recovery_evidence(self):
        from decision_lab.graph_native_lean_v1.session import LeanGraphWitnessSession
        core, _ = self.core(['ns.call', 'other.call', 'target'], [('ns.call', 'target')])
        candidate = SEARCH.TargetedCore.from_loaded(core)
        lean = LeanGraphWitnessSession.__new__(LeanGraphWitnessSession)
        lean.graph_sha256, lean.topology_enabled = core.graph_sha256, True
        result = candidate.graph_find_paths(['call'], ['target'])
        full = lean._normalize(lean._packet(4), result)
        self.assertEqual(full['resolution']['sources'][0]['status'], 'AmbiguousNode')
        self.assertEqual(full['resolution']['sources'][0]['matches'], ['ns.call', 'other.call'])
        compact = SEARCH.compact_packet(full)
        self.assertEqual(compact.get('resolution'), full['resolution'])
        self.assertEqual(compact['next_action'], full['next_action'])


if __name__ == '__main__':
    unittest.main()
