import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support


class SupportTests(unittest.TestCase):
    def test_public_data_does_not_contain_evaluator_fields(self):
        questions, private = support.load_inputs()
        self.assertEqual(len(questions), 50)
        self.assertEqual(len(private), 50)
        self.assertTrue(all(set(q) == {'query_id', 'start', 'target', 'max_hops'} for q in questions))

    def test_primary_exact_set_does_not_require_our_proof_strategy(self):
        q = dict(query_id='x', start='s', target='t', max_hops=4)
        trace = dict(name='get_code_subgraph', arguments={'nodes':['s','t']}, result={
            'status':'ok', 'edges':[{'from':'s','to':'t'}], 'is_truncated':False})
        g = support.grade_episode(q, {'paths':[['s','t']], 'complete':True}, [['s','t']], [trace])
        self.assertTrue(g['exact_set_success'])
        self.assertFalse(g['certified_success'])
        self.assertFalse(g['false_complete_claim'])
        self.assertTrue(g['unverified_complete_claim'])

    def test_complete_neighbor_frontiers_prove_complete(self):
        q = dict(query_id='x', start='s', target='t', max_hops=4)
        obs = [dict(name='get_code_neighbors', arguments={'node':n}, result={
            'status':'ok','node':n,'neighbors':[other],'count':1,'is_truncated':False})
            for n,other in [('s','t'),('t','s')]]
        g = support.grade_episode(q, {'paths':[['s','t']], 'complete':True}, [['s','t']], obs)
        self.assertTrue(g['certified_success'])

    def test_schema_rejects_invalid_keys_and_boolean_integer(self):
        self.assertIsNotNone(support.argument_error('get_code_neighbors', {'node':'s','secret':1}))
        self.assertIsNotNone(support.argument_error('get_code_neighbors', {'node':'s','max_neighbors':True}))
        self.assertIsNotNone(support.argument_error('graph_find_paths', {'source_nodes':['s'],'target_nodes':['t'],'max_hops':5}))
        self.assertIsNone(support.argument_error('get_code_neighbors', {'node':'s','max_neighbors':20000}))

    def test_unknown_id_not_accepted_as_empty_neighborhood_certificate(self):
        q=dict(query_id='x',start='s',target='t',max_hops=4)
        obs=[dict(name='get_code_neighbors',arguments={'node':'unknown'},result={'status':'ok','node':'unknown','neighbors':[],'count':0,'is_truncated':False})]
        self.assertFalse(support.complete_proof(q,obs))

    def test_sources_unchanged(self):
        self.assertGreater(len(support.verify_sources()), 50)


if __name__ == '__main__': unittest.main()
