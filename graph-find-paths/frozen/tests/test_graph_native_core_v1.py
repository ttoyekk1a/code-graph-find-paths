"""Real JSON engineering fixtures, not empirical localization results."""
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


class GraphCoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('decision_lab.graph_native_v1'),
                             'Expected RED: graph_native_v1 feature is missing')
        from decision_lab.graph_native_v1 import GraphWitnessSession
        self.Session = GraphWitnessSession
        self.tmp = tempfile.TemporaryDirectory(prefix='.graph-core-test-',
            dir=pathlib.Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.directory = pathlib.Path(self.tmp.name)

    def graph(self, ids, edges, **kwargs):
        nodes = [dict(id=n, text='stored text for ' + n) for n in ids]
        data = dict(nodes=nodes, edges=edges)
        return self.load(data, **kwargs)

    def load(self, data, **kwargs):
        path = self.directory / ('graph-%s.json' % len(list(self.directory.iterdir())))
        raw = json.dumps(data).encode()
        path.write_bytes(raw)
        session = self.Session(path, hashlib.sha256(raw).hexdigest(), **kwargs)
        return session, data, path

    @staticmethod
    def edge(a, b, **extra):
        return dict(source=a, target=b, key=0, type='calls', **extra)

    def replay(self, result, data):
        rows = data.get('edges', data.get('links'))
        for path in result['paths']:
            self.assertEqual(len(path['steps']) + 1, len(path['node_ids']))
            for i, step in enumerate(path['steps']):
                self.assertEqual([step['from'], step['to']], path['node_ids'][i:i+2])
                self.assertTrue(step['edge_rows'])
                for witness in step['edge_rows']:
                    row = rows[witness['index']]
                    self.assertEqual(witness, dict(index=witness['index'], source=row['source'],
                        target=row['target'], key=row.get('key'), type=row.get('type')))
                    self.assertEqual({row['source'], row['target']}, {step['from'], step['to']})

    def test_bridge_discovered_and_each_step_replays(self):
        session, data, _ = self.graph(['pkg.entry', 'pkg.bridge', 'pkg.target'],
            [self.edge('pkg.entry', 'pkg.bridge'), self.edge('pkg.bridge', 'pkg.target')])
        result = session.graph_find_paths(['pkg.entry'], ['pkg.target'])
        self.assertEqual(result['paths'][0]['node_ids'], ['pkg.entry', 'pkg.bridge', 'pkg.target'])
        self.assertEqual(result['traversal_semantics'], 'stored_adjacency')
        self.assertEqual(result['source_grounding'], 'not_performed')
        self.assertEqual(result['work']['edge_rows_examined'], 4)
        self.assertGreaterEqual(result['setup']['elapsed_seconds'], 0)
        self.assertEqual(result['setup']['edge_rows'], 2)
        self.replay(result, data)

    def test_reciprocals_self_loops_cycles_and_identity(self):
        edges = [self.edge('a', 'b'), self.edge('b', 'a'), self.edge('a', 'a'),
                 self.edge('b', 'c'), self.edge('c', 'a')]
        session, data, _ = self.graph(['a', 'b', 'c'], edges)
        result = session.graph_find_paths(['a'], ['b', 'a'], 4)
        self.assertEqual(len(result['paths']), 1)
        self.assertEqual(len(result['paths'][0]['steps'][0]['edge_rows']), 2)
        self.assertEqual(result['matrix'][1]['state'], 'identity')
        self.assertEqual(result['matrix'][1]['path_ids'], [])
        self.assertEqual(result['work']['visited_nodes'], 3)
        self.assertEqual(result['setup']['canonical_relations'], 4)
        self.replay(result, data)

    def test_resolution_exact_boundary_ambiguous_and_unknown(self):
        session, _, _ = self.graph(['pkg.a.work', 'pkg.b.work', 'pkg.entry', 'xentry'], [])
        result = session.graph_find_paths(['entry'], ['work', 'missing'])
        self.assertEqual(result['resolution']['sources'][0]['node_id'], 'pkg.entry')
        self.assertEqual(result['resolution']['targets'][0]['status'], 'AmbiguousNode')
        self.assertEqual(result['resolution']['targets'][0]['matches'], ['pkg.a.work', 'pkg.b.work'])
        self.assertEqual(result['resolution']['targets'][1]['status'], 'NodeNotInGraph')
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['matrix'][0]['state'], 'unknown')

    def test_suffix_matches_are_bounded_and_truthful(self):
        session, _, _ = self.graph(['p%d.work' % i for i in range(25)] + ['a'], [])
        result = session.graph_find_paths(['a'], ['work'])
        resolution = result['resolution']['targets'][0]
        self.assertEqual(len(resolution['matches']), 8)
        self.assertEqual(resolution['match_count'], 25)
        self.assertEqual(resolution['matches_omitted'], 17)

    def test_links_and_nonstring_node_metadata_preserved(self):
        data = dict(nodes=[dict(id='a', text='stored', attributes=[1, 2]), dict(id='b')],
                    links=[self.edge('a', 'b')])
        session, _, _ = self.load(data)
        self.assertEqual(session.nodes['a'], data['nodes'][0])
        self.assertEqual(session.graph_find_paths(['a'], ['b'])['paths'][0]['node_ids'], ['a', 'b'])

    def test_graph_validation_and_sha_mismatch(self):
        invalid = [dict(nodes=[dict(id='a'), dict(id='a')], edges=[]),
                   dict(nodes=[dict(id=1)], edges=[]), dict(nodes=[dict(id='')], edges=[]),
                   dict(nodes=[dict(id='a')], edges=[self.edge('a', 'b')]),
                   dict(nodes=[dict(id='a')], edges='bad'), dict(nodes='bad', edges=[]),
                   dict(nodes=[dict(id='a')], edges=[dict(source='a')]),
                   dict(nodes=[dict(id='a')], edges=[dict(source=['a'], target='a')])]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)
        path = self.directory / 'bad.json'
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'SHA256Mismatch'):
            self.Session(path, '0' * 64)
        path.write_text('{')
        with self.assertRaisesRegex(ValueError, 'MalformedGraph'):
            self.Session(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_constructor_caps_and_invalid_values(self):
        session, _, path = self.graph(['a'], [])
        sha = session.graph_sha256
        for value in [True, 0, 511, 8001, '8000', 2.5]:
            with self.subTest(limit=value), self.assertRaises(ValueError):
                self.Session(path, sha, output_limit=value)
        with self.assertRaises(ValueError):
            self.Session(path, sha, topology_enabled=1)
        with self.assertRaises(ValueError):
            self.Session(path, 'bad')
        self.assertGreater(self.Session.MAX_FILE_BYTES, 4807863)
        self.assertGreater(self.Session.MAX_NODES, 4106)
        self.assertGreater(self.Session.MAX_EDGES, 2326)
        with self.assertRaisesRegex(ValueError, 'NodeCapExceeded'):
            self.graph(['n%d' % i for i in range(self.Session.MAX_NODES + 1)], [])
        with self.assertRaisesRegex(ValueError, 'EdgeCapExceeded'):
            self.graph(['a'], [self.edge('a', 'a')] * (self.Session.MAX_EDGES + 1))
        huge = self.directory / 'huge.json'
        with huge.open('wb') as stream:
            stream.truncate(self.Session.MAX_FILE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'FileCapExceeded'):
            self.Session(huge, '0' * 64)

    def test_invalid_query_inputs(self):
        session, _, _ = self.graph(['a'], [])
        for bad in [0, 5, True, 1.2, '2', None]:
            with self.subTest(hops=bad), self.assertRaises(ValueError):
                session.graph_find_paths(['a'], ['a'], bad)
        for source, target in [([], ['a']), (['a'] * 4, ['a']), (['a'], []),
                               (['a'], ['a'] * 9), ('a', ['a']), ([1], ['a']),
                               ([''], ['a']), (['a'], [None])]:
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                session.graph_compare_locations(source, target)

    def test_hop_bound_is_completed_but_not_negative_hypothesis_evidence(self):
        session, _, _ = self.graph(['a', 'b', 'c'], [self.edge('a', 'b'), self.edge('b', 'c')])
        result = session.graph_find_paths(['a'], ['c'], 1)
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['matrix'][0]['state'], 'hop_bound')
        self.assertTrue(result['frontier']['complete'])
        self.assertEqual(result['frontier']['boundary_nodes'], 1)
        self.assertEqual(result['preference']['status'], 'unresolved')

    def test_node_budget_shared_by_multiple_anchors(self):
        ids = ['a', 'b'] + ['n%03d' % i for i in range(200)]
        edges = [self.edge('a', n) for n in ids[2:]] + [self.edge('b', n) for n in ids[2:]]
        session, _, _ = self.graph(ids, edges)
        result = session.graph_compare_locations(['a', 'b'], ['n199'], 2)
        self.assertEqual(result['work']['visited_nodes'], 160)
        self.assertLessEqual(result['work']['edge_rows_examined'], 800)
        self.assertFalse(result['frontier']['complete'])
        self.assertTrue(any(c['state'] == 'budget_exhausted' for c in result['matrix']))
        self.assertEqual(result['preference']['status'], 'unresolved')

    def test_edge_budget_shared_and_actual_rows_counted(self):
        edges = [self.edge('a', 'b')] * 1000
        session, _, _ = self.graph(['a', 'b'], edges)
        result = session.graph_compare_locations(['a', 'b'], ['b'], 2)
        self.assertEqual(result['work']['edge_rows_examined'], 800)
        self.assertFalse(result['frontier']['complete'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['preference']['status'], 'unresolved')

    def test_deterministic_bfs_shortest_paths(self):
        session, _, _ = self.graph(['a', 'b', 'c', 'd'],
            [self.edge('a', 'c'), self.edge('c', 'd'), self.edge('a', 'b'), self.edge('b', 'd')])
        results = [session.graph_find_paths(['a'], ['d']) for _ in range(3)]
        self.assertEqual(results[0]['paths'][0]['node_ids'], ['a', 'b', 'd'])
        for result in results[1:]:
            for key in ['paths', 'matrix', 'frontier', 'resolution', 'preference']:
                self.assertEqual(result[key], results[0][key])

    def test_strict_positive_superset_and_tie(self):
        session, _, _ = self.graph(['a1', 'a2', 'both', 'one'],
            [self.edge('a1', 'both'), self.edge('a2', 'both'), self.edge('a1', 'one')])
        result = session.graph_compare_locations(['a1', 'a2'], ['both', 'one'], 1)
        self.assertEqual(result['preference']['status'], 'preferred_for_inspection')
        self.assertEqual(result['preference']['candidate_id'], 'both')
        tied = session.graph_compare_locations(['a1'], ['both', 'one'], 1)
        self.assertEqual(tied['preference']['status'], 'unresolved')
        unknown = session.graph_compare_locations(['a1'], ['both', 'unknown'], 1)
        self.assertEqual(unknown['preference']['status'], 'unresolved')

    def test_incomparable_connections_and_identity_do_not_prefer(self):
        session, _, _ = self.graph(['a1', 'a2', 'c1', 'c2'],
            [self.edge('a1', 'c1'), self.edge('a2', 'c2')])
        result = session.graph_compare_locations(['a1', 'a2'], ['c1', 'c2'], 1)
        self.assertEqual(result['preference']['status'], 'unresolved')
        identity = session.graph_compare_locations(['a1'], ['a1', 'c2'], 1)
        self.assertEqual(identity['preference']['status'], 'unresolved')

    def test_ablation_preserves_text_resolution_and_removes_paths(self):
        session, data, path = self.graph(['pkg.a', 'pkg.bridge', 'pkg.c'],
            [self.edge('pkg.a', 'pkg.bridge'), self.edge('pkg.bridge', 'pkg.c')])
        disabled = self.Session(path, session.graph_sha256, topology_enabled=False)
        enabled = session.graph_find_paths(['a'], ['c'])
        result = disabled.graph_compare_locations(['a'], ['c'])
        self.assertEqual(disabled.nodes, session.nodes)
        self.assertEqual(result['resolution'], enabled['resolution'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['matrix'][0]['state'], 'topology_disabled')
        self.assertEqual(result['work']['edge_rows_examined'], 0)
        self.assertEqual(result['preference']['status'], 'unresolved')
        self.assertEqual(result['ablation_mode'], 'node_only')
        self.assertEqual(result['setup']['edge_rows'], len(data['edges']))

    def test_output_clipping_whole_witnesses_no_dangling_references(self):
        ids = ['anchor'] + ['candidate_%d_' % i + 'z' * 80 for i in range(8)]
        session, data, path = self.graph(ids, [self.edge('anchor', n) for n in ids[1:]], output_limit=4000)
        result = session.graph_find_paths(['anchor'], ids[1:], 1)
        self.assertLessEqual(len(json.dumps(result)), 4000)
        self.assertGreater(result['omissions']['paths'], 0)
        self.assertEqual(result['work']['visited_nodes'], 9)
        self.assertEqual(result['work']['edge_rows_examined'], 8)
        self.replay(result, data)
        delivered = {p['path_id'] for p in result.get('paths', [])}
        for cell in result.get('matrix', []):
            self.assertTrue(set(cell['path_ids']) <= delivered)
        tiny = self.Session(path, session.graph_sha256, output_limit=512).graph_find_paths(['anchor'], ids[1:], 1)
        self.assertEqual(tiny['error'], 'OutputLimitExceeded')
        self.assertLessEqual(len(json.dumps(tiny)), 512)
        self.assertEqual(tiny['work']['visited_nodes'], 9)
        self.assertEqual(tiny['work']['edge_rows_examined'], 8)

    def test_at_most_eight_paths_and_no_references_to_omitted_paths(self):
        anchors = ['a1', 'a2', 'a3']
        targets = ['c%d' % i for i in range(8)]
        session, data, _ = self.graph(anchors + targets,
            [self.edge(a, c) for a in anchors for c in targets])
        result = session.graph_compare_locations(anchors, targets, 1)
        self.assertLessEqual(len(result['paths']), 8)
        self.assertEqual(result['omissions']['paths'], 24 - len(result['paths']))
        delivered = {p['path_id'] for p in result['paths']}
        for cell in result['matrix']:
            self.assertTrue(set(cell['path_ids']) <= delivered)
        self.replay(result, data)

    def test_read_only_bytes_and_no_graph_directory_artifacts(self):
        session, _, path = self.graph(['a', 'b'], [self.edge('a', 'b')])
        before = path.read_bytes()
        entries = sorted(p.name for p in self.directory.iterdir())
        session.graph_find_paths(['a'], ['b'])
        session.graph_compare_locations(['a'], ['b'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), entries)

    def test_bounded_error_preserves_ablation_and_frontier_accounting(self):
        session, _, _ = self.graph(['a', 'b'], [self.edge('a', 'b')],
                                  topology_enabled=False, output_limit=512)
        result = session.graph_find_paths(['a'], ['b'])
        self.assertEqual(result['error'], 'OutputLimitExceeded')
        self.assertIn('ablation_mode', result)
        self.assertEqual(result['ablation_mode'], 'node_only')
        self.assertTrue(result['frontier_complete'])
        self.assertEqual(result['pending_nodes'], 0)
        self.assertGreater(result['setup_elapsed_seconds'], 0)
        self.assertLessEqual(len(json.dumps(result)), 512)

    def test_edge_budget_is_global_across_several_nonparallel_searches(self):
        leaves = ['leaf%03d' % i for i in range(150)]
        session, _, _ = self.graph(['a1', 'a2', 'isolated'] + leaves,
            [self.edge(a, leaf) for a in ['a1', 'a2'] for leaf in leaves])
        result = session.graph_compare_locations(['a1', 'a2'], ['isolated'], 2)
        self.assertEqual(result['work']['visited_nodes'], 152)
        self.assertEqual(result['work']['edge_rows_examined'], 800)
        self.assertFalse(result['frontier']['complete'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['preference']['status'], 'unresolved')

    def test_positive_evidence_during_truncated_search_cannot_prefer(self):
        leaves = ['leaf%03d' % i for i in range(200)]
        ids = ['a', 'b', 'c0', 'c1'] + leaves
        edges = [self.edge('a', 'c0'), self.edge('b', 'c0'), self.edge('a', 'c1')]
        edges.extend(self.edge('a', leaf) for leaf in leaves)
        session, _, _ = self.graph(ids, edges)
        result = session.graph_compare_locations(['a', 'b'], ['c0', 'c1'], 2)
        self.assertTrue(result['paths'])
        self.assertFalse(result['frontier']['complete'])
        self.assertEqual(result['preference']['status'], 'unresolved')

    def test_node_metadata_copy_and_strict_json_validation(self):
        session, data, _ = self.graph(['a'], [])
        nodes = session.nodes
        nodes['a']['text'] = 'changed externally'
        self.assertEqual(session.nodes['a']['text'], data['nodes'][0]['text'])
        for raw in [b'{"nodes":[], "nodes":[], "edges":[]}',
                    b'{"nodes":[], "edges":[], "v":NaN}',
                    b'{"nodes":[], "edges":[], "links":[]}']:
            path = self.directory / 'invalid-raw.json'
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'MalformedGraph'):
                self.Session(path, hashlib.sha256(raw).hexdigest())

    def test_raw_json_numeric_overflow_rejected_in_any_value(self):
        templates = [
            b'{"nodes":[{"id":"a"},{"id":"b"}],"edges":[{"source":"a","target":"b","key":NUMBER}]}',
            b'{"nodes":[{"id":"a"},{"id":"b"}],"edges":[{"source":"a","target":"b","type":NUMBER}]}',
            b'{"nodes":[{"id":"a","metadata":{"nested":[NUMBER]}}],"edges":[]}',
            b'{"nodes":[{"id":"a"}],"edges":[{"source":"a","target":"a","unused":[NUMBER]}]}',
            b'{"nodes":[{"id":"a"}],"edges":[],"unused":{"nested":[NUMBER]}}',
            b'{"nodes":[{"id":"a"},{"id":"b"}],"links":[{"source":"a","target":"b","key":[NUMBER]}]}',
        ]
        for template in templates:
            for number in [b'1e400', b'-1e400']:
                raw = template.replace(b'NUMBER', number)
                path = self.directory / 'overflow-raw.json'
                path.write_bytes(raw)
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'MalformedGraph'):
                    self.Session(path, hashlib.sha256(raw).hexdigest())
                self.assertEqual(path.read_bytes(), raw)

    def test_raw_json_finite_numbers_preserved_and_strictly_serializable(self):
        raw = (b'{"nodes":[{"id":"a","metadata":[1.7976931348623157e308,'
               b'-1.7976931348623157e308,1e-400,-1e-400,1.25,'
               b'123456789012345678901234567890,true,false,null,"1e400"]},'
               b'{"id":"b"}],"edges":[{"source":"a","target":"b",'
               b'"key":1.7976931348623157e308,"type":{"nested":[-2.5e100,0.0]}}]}')
        path = self.directory / 'finite-raw.json'
        path.write_bytes(raw)
        session = self.Session(path, hashlib.sha256(raw).hexdigest())
        data = json.loads(raw)
        self.assertEqual(session.nodes, {node['id']: node for node in data['nodes']})
        result = session.graph_find_paths(['a'], ['b'])
        self.replay(result, data)
        self.assertEqual(json.loads(json.dumps(session.nodes, allow_nan=False)), session.nodes)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertEqual(path.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
