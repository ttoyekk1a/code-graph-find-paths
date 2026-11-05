"""Failures and incomplete token records must stay visible in summary metrics."""
from copy import deepcopy
import unittest
from benchmark50.scripts.aggregate import summarize


def outcome(query_id, *, success, graph, rounds, known, unknown):
    return dict(query_id=query_id, arm='legacy-tools-agent', success=success,
        answer={'paths':[], 'complete':False} if success else None,
        grade=dict(precision=float(success), recall=float(success), certified_success=False,
                   false_complete_claim=False, unverified_complete_claim=False),
        counters=dict(graph_attempts=graph, python_attempts=0, model_rounds=rounds),
        usage=dict(prompt_tokens=known, completion_tokens=0, unknown_usage_responses=unknown))


class AggregateTests(unittest.TestCase):
    def test_failures_participate_in_means_and_partial_uses_fifty_denominator(self):
        rows = [outcome('q1', success=True, graph=2, rounds=1, known=10, unknown=0),
                outcome('q2', success=False, graph=4, rounds=2, known=20, unknown=1)]
        group = summarize(rows)['Competition tools']
        self.assertEqual(group['started'], 2)
        self.assertEqual(group['missing'], 48)
        self.assertEqual(group['status'], 'partial')
        self.assertEqual(group['success_rate'], 1/50)
        self.assertEqual(group['mean_denominator'], 2)
        self.assertEqual(group['mean_graph_attempts'], 3)
        self.assertEqual(group['mean_model_rounds'], 1.5)
        self.assertEqual(group['mean_path_recall'], .5)
        self.assertEqual(group['total_tokens_known'], 30)
        self.assertEqual(group['mean_tokens_known'], 15)
        self.assertEqual(group['unknown_usage_responses'], 1)
        self.assertFalse(group['token_usage_complete'])

    def test_all_fifty_outcomes_can_still_have_unknown_usage(self):
        rows = [outcome('q'+str(i), success=False, graph=0, rounds=1, known=0, unknown=int(i==0))
                for i in range(50)]
        group = summarize(rows)['Competition tools']
        self.assertEqual(group['status'], 'complete')
        self.assertEqual(group['missing'], 0)
        self.assertEqual(group['success_rate'], 0)
        self.assertFalse(group['token_usage_complete'])
        rows[0]['usage']['unknown_usage_responses'] = 0
        self.assertTrue(summarize(rows)['Competition tools']['token_usage_complete'])

    def test_duplicate_episode_cannot_inflate_summary(self):
        row = outcome('q', success=True, graph=1, rounds=1, known=1, unknown=0)
        with self.assertRaises(ValueError):
            summarize([row, deepcopy(row)])


if __name__ == '__main__':
    unittest.main()
