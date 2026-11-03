"""Lean adapter acceptance uses the unchanged core on disposable graphs."""
import copy
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from decision_lab.graph_native_v1.core import GraphWitnessSession
from decision_lab.graph_native_v1.source_read import live_identity


class BoundaryContext:
    """Only budget, lock and runtime identity, never a graph/source simulator."""
    def __init__(self):
        self.tool_lock = threading.RLock()
        self.task_id = 'generic-task'
        self.container_id = 'generic-container'
        self.sandbox = SimpleNamespace(sandbox_id='owned', work_dir='/workspace')
        self.tool_calls_used = 0
        self.gate_calls = 0
        self.budget = SimpleNamespace(tool_calls=8)

    def check_budget(self, *, count_tool_call=True):
        self.gate_calls += 1
        if self.tool_calls_used >= self.budget.tool_calls:
            return json.dumps(dict(status='error', error_type='BudgetExceeded'))
        if count_tool_call:
            self.tool_calls_used += 1
        return None


class LeanSessionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.mod = importlib.import_module('decision_lab.graph_native_lean_v1')
            self.session_type = self.mod.LeanGraphWitnessSession
        except (ImportError, AttributeError):
            self.fail('Expected RED: LeanGraphWitnessSession is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = BoundaryContext()
        self.identity = dict(identity=live_identity(self.ctx, '/workspace'), ownership=dict(
            attested_owned=True, owner='tests', record_id='generic', task_id=self.ctx.task_id,
            container_id=self.ctx.container_id, workspace_root='/workspace'))

    def session(self, ids=None, rows=None, **options):
        ids = ids or ['g.a', 'g.b', 'g.c', 'g.d', 'g.e']
        if rows is None:
            rows = [dict(source='g.c', target='g.a', key=0, type='stored'),
                    dict(source='g.a', target='g.d', key=1, type='stored'),
                    dict(source='g.b', target='g.c', key=2, type='stored'),
                    dict(source='g.d', target='g.b', key=3, type='stored')]
        path = self.root / ('graph' + str(len(list(self.root.iterdir()))) + '.json')
        path.write_text(json.dumps(dict(nodes=[dict(id=x, text='stored') for x in ids], links=rows)))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        session = self.session_type(self.ctx, graph_path=path, expected_graph_sha256=sha,
                                   runtime_identity=self.identity, **options)
        self.addCleanup(session.close)
        return session

    def call(self, session, left=None, right=None, hops=4, compare=True):
        factory = self.mod.make_graph_compare_locations if compare else self.mod.make_graph_find_paths
        return factory(session)(left or ['g.a', 'g.b'], right or ['g.c', 'g.d', 'g.e'], hops)

    def bounded(self, result, limit=8000):
        raw = json.dumps(result)
        self.assertLessEqual(len(raw), limit)
        self.assertEqual(result['accounting']['output_characters'], len(raw))
        self.assertEqual(result['accounting']['output_bytes'], len(raw.encode('utf-8')))
        delivered = {p['path_id'] for p in result['paths']}
        for cell in result['matrix']:
            self.assertTrue(set(cell['path_ids']) <= delivered)
            if cell['state'] == 'connected':
                self.assertTrue(cell['path_ids'])
            else:
                self.assertEqual(cell['path_ids'], [])

    def test_synthetic_two_by_three_replayable_support_one_core_call(self):
        session = self.session()
        original = session.core.graph_compare_locations
        with patch.object(session.core, 'graph_compare_locations', wraps=original) as query:
            result = self.call(session)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.ctx.gate_calls, 1)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([c['state'] for c in result['matrix']],
                         ['connected', 'connected', 'no_path_within_bound'] * 2)
        self.assertEqual(len(result['paths']), 4)
        rows = json.loads(Path(session.core.graph_path).read_text())['links']
        for path in result['paths']:
            for i, step in enumerate(path['steps']):
                self.assertEqual([step['from'], step['to']], path['node_ids'][i:i+2])
                for row in step['edge_rows']:
                    original = rows[row['index']]
                    self.assertEqual({k: row[k] for k in ('source', 'target', 'key', 'type')}, original)
        self.assertEqual(result['source_grounding'], 'not_performed')
        self.assertEqual(result['accounting']['core_calls'], 1)
        self.assertEqual(result['omissions']['paths'], 0)
        for forbidden in ('preference', 'bindings', 'provenance', 'setup', 'snippets'):
            self.assertNotIn(forbidden, result)
        self.bounded(result)

    def test_c0_uses_same_frozen_graph_without_nodes_copy_or_sources(self):
        with patch.object(GraphWitnessSession, 'nodes', property(lambda _: self.fail('all-node copy'))):
            enabled = self.session()
            disabled = self.session(topology_enabled=False)
        self.assertEqual(enabled.core._nodes, disabled.core._nodes)
        result = self.call(disabled, ['g.a'], ['g.a', 'g.c'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'topology_disabled')
        self.assertTrue(all(c['state'] == 'unknown' for c in result['matrix']))
        self.assertEqual(result['accounting']['graph']['edge_rows_examined'], 0)
        self.assertEqual(disabled.setup['source_reads'], 0)
        self.assertEqual(disabled.setup['source_parses'], 0)
        self.assertEqual(disabled.setup['nodes_copy_count'], 0)
        self.assertEqual(disabled.setup['core_setup']['nodes'], 5)
        self.bounded(result)

    def test_global_edge_budget_retains_positive_and_completed_negative(self):
        rows = [dict(source='g.b', target='g.c', key=i, type='parallel') for i in range(900)]
        session = self.session(rows=rows)
        result = self.call(session, ['g.a', 'g.b'], ['g.c', 'g.e'])
        self.assertEqual(result['accounting']['graph']['edge_rows_examined'], 800)
        self.assertEqual(result['accounting']['core_calls'], 1)
        self.assertEqual([c['state'] for c in result['matrix']],
                         ['no_path_within_bound', 'no_path_within_bound', 'unknown', 'unknown'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.ctx.gate_calls, 1)
        self.bounded(result)

    def test_positive_witness_remains_when_frontier_incomplete(self):
        rows = [dict(source='g.a', target='g.c', key=0, type='edge')]
        rows += [dict(source='g.b', target='g.d', key=i, type='parallel') for i in range(900)]
        result = self.call(self.session(rows=rows), ['g.a', 'g.b'], ['g.c', 'g.e'])
        self.assertEqual(result['matrix'][0]['state'], 'connected')
        self.assertEqual(result['matrix'][-1]['state'], 'unknown')
        self.assertEqual(result['status'], 'partial')
        self.bounded(result)

    def test_global_visited_cap_never_multiplies_per_anchor(self):
        ids = ['g.a', 'g.b'] + ['g.n' + str(i) for i in range(200)]
        rows = [dict(source='g.b', target=node) for node in ids[2:]]
        result = self.call(self.session(ids, rows), ['g.a', 'g.b'], ['g.n99'])
        self.assertEqual(result['accounting']['graph']['visited_nodes'], 160)
        self.assertLessEqual(result['accounting']['graph']['edge_rows_examined'], 800)
        self.assertEqual(result['matrix'][0]['state'], 'no_path_within_bound')
        self.assertEqual(result['matrix'][1]['state'], 'unknown')
        self.bounded(result)

    def test_resolution_identity_ambiguity_missing_and_duplicates(self):
        ids = ['p.a', 'q.a', 'g.b', 'g.c']
        rows = [dict(source='p.a', target='g.b')]
        session = self.session(ids, rows)
        result = self.call(session, ['p.a', 'a', 'missing'], ['p.a', 'b', 'b'])
        self.assertEqual(result['matrix'][0]['reason'], 'identity_only_no_edge_support')
        self.assertEqual([c['state'] for c in result['matrix'][:3]], ['unknown', 'connected', 'connected'])
        self.assertTrue(all(c['state'] == 'unknown' for c in result['matrix'][3:]))
        self.assertEqual(result['resolution']['sources'][1]['status'], 'AmbiguousNode')
        self.assertEqual(len(result['paths']), 1)
        self.bounded(result)

    def test_invalid_lists_bool_hops_and_bounds_no_core_call(self):
        session = self.session()
        cases = [('g.a', ['g.c'], 4), ([], ['g.c'], 4), (['g.a'], ['g.c'], True),
                 (['g.a'], ['g.c'], 0), (['g.a'], ['g.c'], 5), ([False], ['g.c'], 4),
                 (['g.a'] * 4, ['g.c'], 4), (['g.a'], ['g.c'] * 9, 4)]
        with patch.object(session.core, 'graph_find_paths', wraps=session.core.graph_find_paths) as query:
            for left, right, hops in cases:
                result = self.mod.make_graph_find_paths(session)(left, right, hops)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['error_type'], 'InvalidArguments')
                self.assertEqual(result['accounting']['core_calls'], 0)
                self.bounded(result)
        self.assertEqual(query.call_count, 0)
        self.assertEqual(self.ctx.gate_calls, len(cases))

    def test_core_path_cap_whole_path_omission_no_dangling_ids(self):
        ids = ['g.a', 'g.b', 'g.c'] + ['g.t' + str(i) for i in range(5)]
        rows = [dict(source=a, target=t) for a in ids[:3] for t in ids[3:]]
        result = self.call(self.session(ids, rows), ids[:3], ids[3:])
        self.assertLessEqual(len(result['paths']), 8)
        self.assertTrue(result['omissions']['paths'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['omissions']['paths'], 15-len(result['paths']))
        self.bounded(result)

    def test_lean_final_limit_drops_only_whole_paths(self):
        session = self.session(output_limit=2800)
        result = self.call(session)
        self.assertTrue(result['omissions']['paths'])
        for path in result['paths']:
            self.assertEqual(len(path['steps']), len(path['node_ids'])-1)
            self.assertTrue(all(step['edge_rows'] for step in path['steps']))
        self.assertEqual(result['omissions']['paths'], 4-len(result['paths']))
        self.bounded(result, 2800)

    def test_huge_ids_core_error_and_huge_rows_never_truncate_ids(self):
        huge = 'g.' + 'x' * 10000
        session = self.session(['g.a', huge], [dict(source='g.a', target=huge)])
        result = self.call(session, ['g.a'], [huge])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'OutputLimitExceeded')
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['matrix'], [])
        self.assertTrue(result['omissions']['whole_payload'])
        self.assertGreater(result['accounting']['graph']['edge_rows_examined'], 0)
        self.bounded(result)
        result = self.call(self.session(rows=[dict(source='g.a', target='g.c', key='y'*12000)]), ['g.a'], ['g.c'])
        self.assertEqual(result['matrix'][0]['state'], 'unknown')
        self.assertEqual(result['omissions']['paths'], 1)
        self.bounded(result)

    def test_gate_rejection_once_preserves_work_counters(self):
        session = self.session()
        self.ctx.tool_calls_used = 8
        result = self.call(session)
        self.assertEqual(self.ctx.gate_calls, 1)
        self.assertEqual(result['error_type'], 'BudgetExceeded')
        self.assertEqual(result['accounting']['core_calls'], 0)
        self.assertEqual(result['accounting']['native_budget_decision'], 'rejected')
        self.assertEqual(session.native, dict(attempts=1, admitted=0, rejected=1))
        self.assertEqual(result['accounting']['graph']['edge_rows_examined'], 0)
        self.bounded(result)

    def test_unexpected_after_work_failure_reports_unknown_not_zero(self):
        session = self.session()
        real = session.core.graph_compare_locations
        def failure(*args):
            real(*args)
            raise RuntimeError('/private/never disclose this')
        with patch.object(session.core, 'graph_compare_locations', side_effect=failure):
            result = self.call(session)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'CoreExecutionError')
        self.assertIsNone(result['accounting']['graph']['edge_rows_examined'])
        self.assertTrue(result['accounting']['graph']['incomplete'])
        self.assertIsNone(session.cumulative['edge_rows_examined'])
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['accounting']['core_calls'], 1)
        self.bounded(result)

    def test_runtime_change_before_and_after_core_poisons(self):
        session = self.session()
        self.ctx.container_id = 'changed'
        result = self.call(session)
        self.assertEqual(result['error_type'], 'RuntimeChanged')
        self.assertEqual(result['accounting']['core_calls'], 0)
        self.ctx.container_id = 'generic-container'
        self.assertEqual(self.call(session)['error_type'], 'RuntimeChanged')
        session = self.session()
        real = session.core.graph_compare_locations
        def change(*args):
            result = real(*args)
            self.ctx.container_id = 'changed'
            return result
        with patch.object(session.core, 'graph_compare_locations', side_effect=change):
            result = self.call(session)
        self.assertEqual(result['error_type'], 'RuntimeChanged')
        self.assertEqual(result['matrix'], [])
        self.assertGreater(result['accounting']['graph']['edge_rows_examined'], 0)
        self.bounded(result)

    def test_post_core_runtime_change_counts_retained_and_core_omitted_paths(self):
        ids = ['g.a', 'g.b', 'g.c'] + ['g.t' + str(i) for i in range(5)]
        rows = [dict(source=a, target=t) for a in ids[:3] for t in ids[3:]]
        session = self.session(ids, rows)
        real = session.core.graph_compare_locations
        def change(*args):
            result = real(*args)
            self.ctx.container_id = 'changed'
            return result
        with patch.object(session.core, 'graph_compare_locations', side_effect=change) as query:
            result = self.call(session, ids[:3], ids[3:])
        core = session.audit_ledger[0]['core_result']
        self.assertEqual(len(core['paths']), 8)
        self.assertEqual(core['omissions']['paths'], 7)
        self.assertEqual(result['omissions']['paths'], 15)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'RuntimeChanged')
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.ctx.gate_calls, 1)
        self.assertEqual(result['accounting']['core_calls'], 1)
        self.assertEqual(result['accounting']['native_budget_decision'], 'admitted')
        self.assertEqual(result['accounting']['graph']['edge_rows_examined'], 90)
        self.assertEqual(session.cumulative['edge_rows_examined'], 90)
        self.assertFalse(result['accounting']['graph']['incomplete'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['matrix'], [])
        self.assertEqual(result['resolution'], dict(sources=[], targets=[]))
        self.assertIsNone(result['frontier'])
        self.assertTrue(result['omissions']['whole_payload'])
        self.bounded(result)
        self.ctx.container_id = 'generic-container'
        poisoned = self.call(session, ids[:3], ids[3:])
        self.assertEqual(poisoned['error_type'], 'RuntimeChanged')
        self.assertEqual(poisoned['accounting']['core_calls'], 0)
        self.assertEqual(self.ctx.gate_calls, 2)

    def test_post_core_runtime_change_preserves_unknown_mandatory_error_omissions(self):
        huge = 'g.' + 'x' * 10000
        session = self.session(['g.a', huge], [dict(source='g.a', target=huge)])
        real = session.core.graph_compare_locations
        def change(*args):
            result = real(*args)
            self.ctx.container_id = 'changed'
            return result
        with patch.object(session.core, 'graph_compare_locations', side_effect=change) as query:
            result = self.call(session, ['g.a'], [huge])
        core = session.audit_ledger[0]['core_result']
        self.assertEqual(core['error'], 'OutputLimitExceeded')
        self.assertNotIn('omissions', core)
        self.assertIsNone(result['omissions']['paths'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'RuntimeChanged')
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.ctx.gate_calls, 1)
        self.assertEqual(result['accounting']['core_calls'], 1)
        self.assertEqual(result['accounting']['native_budget_decision'], 'admitted')
        self.assertEqual(result['accounting']['graph']['edge_rows_examined'], 2)
        self.assertEqual(session.cumulative['edge_rows_examined'], 2)
        self.assertFalse(result['accounting']['graph']['incomplete'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['matrix'], [])
        self.assertEqual(result['resolution'], dict(sources=[], targets=[]))
        self.assertIsNone(result['frontier'])
        self.assertTrue(result['omissions']['whole_payload'])
        self.bounded(result)
        self.ctx.container_id = 'generic-container'
        poisoned = self.call(session, ['g.a'], [huge])
        self.assertEqual(poisoned['error_type'], 'RuntimeChanged')
        self.assertEqual(poisoned['accounting']['core_calls'], 0)
        self.assertEqual(self.ctx.gate_calls, 2)

    def test_defensive_snapshots_audit_and_idempotent_close(self):
        session = self.session()
        result = self.call(session)
        for prop in ('setup', 'native', 'cumulative'):
            snapshot = getattr(session, prop)
            snapshot.clear()
            self.assertTrue(getattr(session, prop))
        audit = session.audit_ledger
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]['core_result_already_bounded'])
        self.assertTrue(audit[0]['core_result']['paths'])
        self.assertEqual(audit[0]['final_output_characters'], len(json.dumps(result)))
        audit[0].clear()
        self.assertTrue(session.audit_ledger[0])
        session.close(); session.close()
        result = self.call(session)
        self.assertEqual(result['error_type'], 'SessionClosed')
        self.assertEqual(len(session.audit_ledger), 2)
        self.assertEqual(self.ctx.gate_calls, 2)
        self.bounded(result)

    def test_audit_records_core_normalization_and_output_costs_before_projection(self):
        session = self.session(output_limit=2800)
        result = self.call(session)
        audit = session.audit_ledger[0]
        self.assertIn('core_output_characters', audit, 'Core output measurement is required')
        self.assertLessEqual(audit['core_output_characters'], 6000)
        self.assertEqual(audit['core_output_characters'], len(json.dumps(audit['core_result'])))
        for field in ('core_call_seconds', 'core_result_copy_seconds', 'normalization_seconds',
                      'native_serialization_seconds', 'pre_limiter_snapshot_seconds', 'final_projection_seconds'):
            self.assertGreaterEqual(audit[field], 0)
        self.assertGreater(len(audit['normalized_before_final_limiter']['paths']), len(result['paths']))

    def test_mandatory_metadata_fallback_is_error_and_preserves_actual_work(self):
        ids = ['g.a', 'g.b', 'g.c'] + ['g.t' + str(i) for i in range(8)]
        rows = [dict(source=a, target=t) for a in ids[:3] for t in ids[3:]]
        session = self.session(ids, rows, output_limit=2048)
        result = self.call(session, ids[:3], ids[3:])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'OutputLimitExceeded')
        self.assertTrue(result['omissions']['whole_payload'])
        self.assertEqual(result['matrix'], [])
        self.assertEqual(result['resolution'], dict(sources=[], targets=[]))
        self.assertGreater(result['accounting']['graph']['edge_rows_examined'], 0)
        self.assertEqual(result['accounting']['core_calls'], 1)
        self.bounded(result, 2048)

    def test_native_gate_and_accounting_are_atomic_across_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        session = self.session()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call(session), range(12)))
        self.assertEqual(self.ctx.gate_calls, 12)
        self.assertEqual(self.ctx.tool_calls_used, 8)
        self.assertEqual(session.native, dict(attempts=12, admitted=8, rejected=4))
        self.assertEqual(sum(r['accounting']['core_calls'] for r in results), 8)
        self.assertEqual(len(session.audit_ledger), 12)
        for result in results:
            self.bounded(result)

    def test_non_ascii_ids_use_default_json_exact_bytes(self):
        ids = ['g.\u6839', 'g.\u8449']
        session = self.session(ids, [dict(source=ids[1], target=ids[0], type='\u96a3\u63a5')])
        result = self.call(session, [ids[0]], [ids[1]])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['paths'][0]['node_ids'], ids)
        self.bounded(result)

    def test_four_status_action_sentences_follow_fixed_public_catalog(self):
        session = self.session()
        result = self.call(session)
        self.assertEqual(result['next_action'], 'Use the returned paths and bounded absence cells as graph evidence; submit only claims they support.')
        self.assertNotIn('error_type', result)
        result = self.call(self.session(topology_enabled=False))
        self.assertEqual(result['next_action'], 'No edge evidence is available in this mode; submit unavailable for tasks requiring stored graph connections.')
        result = self.mod.make_graph_find_paths(session)(['g.a'], ['g.c'], True)
        self.assertEqual(result['next_action'], 'Supply bounded lists of nonempty node IDs and an integer max_hops from 1 to 4; booleans are invalid.')
        self.assertLessEqual(len(result['next_action']), 240)

    def test_unexpected_final_packaging_failure_keeps_audit_and_known_work(self):
        session = self.session()
        with patch.object(session, '_bound', side_effect=RuntimeError('/private/packaging failed')):
            result = self.call(session)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'PackagingError')
        self.assertEqual(result['next_action'], 'Evidence packaging failed; do not infer graph claims from the omitted result.')
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['matrix'], [])
        self.assertTrue(result['omissions']['whole_payload'])
        self.assertGreater(result['accounting']['graph']['edge_rows_examined'], 0)
        self.assertEqual(result['accounting']['core_calls'], 1)
        self.assertEqual(len(session.audit_ledger), 1)
        self.assertTrue(session.audit_ledger[0]['core_result']['paths'])
        self.assertNotIn('private', json.dumps(result))
        self.bounded(result)

    def test_actual_factory_signatures_and_adk_schema(self):
        from google.adk.tools import FunctionTool
        session = self.session()
        for factory, names in [(self.mod.make_graph_find_paths, ['source_nodes', 'target_nodes', 'max_hops']),
                               (self.mod.make_graph_compare_locations, ['anchor_nodes', 'candidate_nodes', 'max_hops'])]:
            fn = factory(session)
            self.assertEqual(list(inspect.signature(fn).parameters), names)
            self.assertEqual(inspect.signature(fn).parameters['max_hops'].default, 4)
            schema = FunctionTool(fn)._get_declaration().model_dump(mode='json', by_alias=True, exclude_none=True)
            self.assertEqual(schema['name'], fn.__name__)
            self.assertEqual(list(schema['parameters']['properties']), names)
            self.assertIn('unknown', fn.__doc__)
            self.assertIn('source_grounding=not_performed', fn.__doc__)


if __name__ == '__main__':
    unittest.main()
