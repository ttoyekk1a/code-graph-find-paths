import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import episode
import run_agents
from test_episode import Backend, Python, call, response
from support import CONFIG

QUESTION = dict(query_id='q', start='s', target='t', max_hops=4)
REF = dict(expected_paths=[['s', 't']], graph_raw_sha256='hash', instance_id='i', cohort='test')
ARM = 'legacy-tools-agent'


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.backend = Backend()
        self.python = Python()

    def run_episode(self, *responses, config=None):
        script = iter(responses)
        def transport(*_):
            item = next(script)
            if isinstance(item, BaseException):
                raise item
            return item
        return episode.run_episode(QUESTION, ARM, self.backend, self.python, transport,
                                   self.root/'episode', config)

    def test_malformed_batch_counts_emissions_and_caps_invalid_attempts(self):
        a = call('get_code_neighbors', {}, 'duplicate')
        a['function']['arguments'] = {'node': 's'}
        b = call('get_code_neighbors', {'node': 's'}, 'duplicate')
        c = call('execute_python', {'code': 'print(1)'})
        raw = response(a, b, c)
        row = self.run_episode(raw, config={**CONFIG, 'max_graph_queries': 1})
        self.assertEqual(row['stop_reason'], 'model_format_error')
        self.assertEqual(row['counters']['tool_calls_emitted'], 3)
        self.assertEqual(row['counters']['graph_emitted'], 2)
        self.assertEqual(row['counters']['python_emitted'], 1)
        self.assertEqual(row['counters']['graph_attempts'], 1)
        self.assertEqual(row['counters']['python_attempts'], 0)
        self.assertEqual(row['traces'][0]['arguments_raw'], {'node': 's'})
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.python.inputs, [])
        self.assertEqual(json.loads((self.root/'episode/rounds/001/response.json').read_text()), raw)

    def test_campaign_continues_after_malformed_model_output(self):
        malformed = call('get_code_neighbors', {})
        malformed['function']['arguments'] = {}
        class Transport:
            def __init__(self, _):
                self.script = iter([response(malformed), response()])
            def __call__(self, *_):
                return next(self.script)
            def close(self):
                pass
        run = self.root/'campaign'
        with patch.multiple(run_agents, freeze_files=lambda: {}, load_inputs=lambda: ([QUESTION], {'q': REF}),
                            launch_gate=lambda *_: {}, run_path=lambda _: run, ChatTransport=Transport,
                            schedule=lambda _: [(QUESTION, ARM), (QUESTION, 'guided-legacy-tools-agent')],
                            make_backend=lambda *_: self.backend, DockerPython=lambda *_: self.python):
            with contextlib.redirect_stdout(io.StringIO()):
                run_agents.main(['--execute', '--run-name', 'test'])
        completion = json.loads((run/'completion.json').read_text())
        self.assertEqual(completion['finished_tasks'], 2)
        self.assertEqual(completion['reason'], 'completed')

    def test_rejected_submission_diagnostics_survive_collection(self):
        attempted = {'paths': [['s', 't'], ['s', 't'], ['wrong', 't']], 'complete': True}
        row = self.run_episode(response(call('get_code_neighbors', {'node': 's'}),
                                        call('submit_graph_answer', attempted, 'submit')))
        self.assertIsNone(row['answer'])
        self.assertEqual(row['submitted_answer'], attempted)
        evaluated, = run_agents.collect_rows(self.root, {'q': REF})
        self.assertFalse(evaluated['success'])
        grade = evaluated['grade']
        self.assertEqual(grade['duplicate_path_count'], 1)
        self.assertEqual(grade['invalid_path_count'], 1)
        self.assertEqual(grade['matched_path_count'], 1)
        self.assertEqual(grade['recall'], 1)
        self.assertTrue(grade['submission_gradable'])
        self.assertEqual(grade['missing_path_count'], 0)
        self.assertTrue(grade['complete_claim'])
        self.assertTrue(grade['false_complete_claim'])

    def test_malformed_submission_json_is_ungradable(self):
        row = self.run_episode(response(call('submit_graph_answer', '{')))
        self.assertEqual(row['traces'][0]['result']['error_type'], 'invalid_json')
        self.assertIsNone(row['submitted_answer'])
        evaluated = run_agents.evaluate(row, REF)
        self.assertFalse(evaluated['grade']['submission_gradable'])
        self.assertEqual(evaluated['grade']['missing_path_count'], 1)

    def test_pending_interrupt_counts_one_unknown_after_known_response(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_episode(response(call('get_code_neighbors', {'node': 's'})), KeyboardInterrupt())
        row = json.loads((self.root/'episode/result.json').read_text())
        self.assertEqual(row['stop_reason'], 'interrupted')
        self.assertEqual(row['counters']['model_rounds'], 2)
        self.assertEqual(row['usage'], dict(prompt_tokens=10, completion_tokens=5, unknown_usage_responses=1))

    def test_known_usage_survives_interrupt_while_saving_response(self):
        original_save = episode.save
        def save(path, value):
            if path.name == 'response.json':
                raise SystemExit()
            return original_save(path, value)
        with patch.object(episode, 'save', save), self.assertRaises(SystemExit):
            self.run_episode(response())
        row = json.loads((self.root/'episode/result.json').read_text())
        self.assertEqual(row['usage'], dict(prompt_tokens=10, completion_tokens=5, unknown_usage_responses=0))

    def test_python_prefix_fits_wrapped_limit_preserving_cleanup(self):
        raw = {'status': 'timeout', 'stdout': '"雪'*3000, 'stderr': 'x'*35000, 'cleanup': 'unknown'}
        raw['stderr'] += 'x'*(64000-len(json.dumps(raw)))
        self.assertEqual(len(json.dumps(raw)), 64000)
        with patch.object(Python, 'execute', return_value=raw):
            row = self.run_episode(response(call('execute_python', {'code': 'print(1)'})))
        trace, = row['traces']
        result = trace['result']
        self.assertEqual(row['stop_reason'], 'python_cleanup_unconfirmed')
        self.assertLessEqual(len(trace['delivered_text']), 64000)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['original_status'], 'timeout')
        self.assertEqual(result['cleanup'], 'unknown')
        self.assertTrue(result['output_truncated'])
        self.assertEqual(result['stdout'], raw['stdout'])
        self.assertTrue(raw['stderr'].startswith(result['stderr']))
        self.assertGreater(len(result['stderr']), 39000)
        self.assertEqual(trace['raw_result'], raw)
