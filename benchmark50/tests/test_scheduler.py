"""Offline campaign scheduling, recovery and unknown-request accounting guards."""
from concurrent.futures import Future
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark50.scripts import run

QUESTION = dict(query_id='q', start='s', target='t', max_hops=4)
REF = dict(**QUESTION, expected_paths=[['s','t']], graph_raw_sha256='pinned', instance_id='i', cohort='test')
ARMS = list(run.CONFIG['arms'])


def row_for(arm, stop='submitted'):
    return dict(query_id='q', arm=arm, public_question=deepcopy(QUESTION), answer=None,
                stop_reason=stop, observations=[], error=None,
                counters=dict(model_rounds=1, graph_attempts=0, python_attempts=0),
                usage=dict(prompt_tokens=10, completion_tokens=5, unknown_usage_responses=0))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root/'run'
        self.args = SimpleNamespace(python_image='sha256:'+'0'*64, docker_host=None,
            run_name='run', runs_dir=self.root, implementation='historical', resume=False, workers=2)

    def harness(self, *, stop=None, crash=False):
        owner = self
        self.submissions = []
        self.waits = []
        self.unfinished = {}
        class ControlledPool:
            def __init__(self, **kwargs):
                owner.assertEqual(kwargs['max_tasks_per_child'], 1)
                owner.assertEqual(kwargs['mp_context'].get_start_method(), 'spawn')
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, job):
                future = Future()
                owner.submissions.append(job[2])
                owner.unfinished[future] = (fn, job)
                owner.assertLessEqual(sum(not f.done() for f in owner.unfinished), owner.args.workers,
                                      'Scheduler submitted more than the available worker slots')
                return future
        def wait(futures, **kwargs):
            self.waits.append(len(futures))
            future = next(iter(futures))
            fn, job = self.unfinished[future]
            try:
                future.set_result(fn(job))
            except Exception as exc:
                future.set_exception(exc)
            return {future}, set(futures)-{future}
        def worker(job):
            path = Path(job[3]);path.mkdir()
            if crash:
                (path/'request-started.json').write_text('{}')
                raise RuntimeError('worker failed after a request may have started')
            row = row_for(job[2], stop or 'submitted')
            (path/'result.json').write_text(json.dumps(row))
            return row
        python = SimpleNamespace(execute=lambda *a,**k: {'status':'ok', 'cleanup':'confirmed'})
        return patch.multiple(run, ProcessPoolExecutor=ControlledPool, wait=wait, worker=worker,
                              DockerPython=lambda *a,**k: python, input_identity=lambda *_: {})

    def test_only_free_slots_are_submitted_then_refilled(self):
        with self.harness():
            run.model_campaign(self.args, [QUESTION], {'q':REF}, {})
        self.assertCountEqual(self.submissions, ARMS)
        self.assertTrue(self.waits)
        self.assertLessEqual(max(self.waits), self.args.workers)

    def test_cleanup_unknown_stops_before_submitting_remaining_tasks(self):
        with self.harness(stop='python_cleanup_unconfirmed'):
            with self.assertRaisesRegex(RuntimeError, '[Cc]leanup'):
                run.model_campaign(self.args, [QUESTION], {'q':REF}, {})
        self.assertEqual(len(self.submissions), self.args.workers)
        self.assertNotIn(ARMS[2], self.submissions)

    def test_interruption_cancels_unstarted_futures_and_never_fills_more_slots(self):
        with self.harness(), patch.object(run, 'wait', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run.model_campaign(self.args, [QUESTION], {'q':REF}, {})
        self.assertEqual(len(self.submissions), self.args.workers)
        self.assertTrue(all(future.cancelled() for future in self.unfinished))
        completion = json.loads((self.out/'completion.json').read_text())
        self.assertFalse(completion['request_count_complete'])
        self.assertTrue(completion['unknown_execution_tasks'])

    def prepare_resume(self):
        self.out.mkdir()
        self.args.resume = True
        identity = dict(config=run.effective_config(self.args.python_image), connection={},
                        implementation='historical', input_hashes={})
        (self.out/'manifest.json').write_text(json.dumps({'identity':identity}))

    def test_resume_rejects_bad_directory_identity_before_any_preflight_or_worker(self):
        self.prepare_resume()
        path = self.out/('q--'+ARMS[1]);path.mkdir()
        (path/'result.json').write_text(json.dumps(row_for(ARMS[0])))
        with patch.object(run, 'input_identity', return_value={}), \
             patch.object(run, 'DockerPython') as python, \
             patch.object(run, 'ProcessPoolExecutor') as pool:
            with self.assertRaises(ValueError):
                run.model_campaign(self.args, [QUESTION], {'q':REF}, {})
            python.assert_not_called();pool.assert_not_called()

    def test_resume_never_reexecutes_existing_complete_or_partial_directory(self):
        self.prepare_resume()
        complete = self.out/('q--'+ARMS[0]);complete.mkdir()
        (complete/'result.json').write_text(json.dumps(row_for(ARMS[0])))
        partial = self.out/('q--'+ARMS[1]);partial.mkdir()
        (partial/'request-started.json').write_text('{}')
        with self.harness():
            run.model_campaign(self.args, [QUESTION], {'q':REF}, {})
        self.assertEqual(self.submissions, [ARMS[2]])
        completion = json.loads((self.out/'completion.json').read_text())
        self.assertFalse(completion['request_count_complete'])
        self.assertTrue(completion['unknown_execution_tasks'])

    def test_worker_exception_preserves_unknown_request_accounting(self):
        with self.harness(crash=True):
            with self.assertRaisesRegex(RuntimeError, '[Ww]orker'):
                run.model_campaign(self.args, [QUESTION], {'q':REF}, {})
        self.assertEqual(len(self.submissions), self.args.workers)
        self.assertEqual(len(list(self.out.glob('*.worker-error.json'))), 1)
        completion = json.loads((self.out/'completion.json').read_text())
        self.assertEqual(completion['known_model_requests'], completion['model_requests'])
        self.assertEqual(completion['known_model_requests'], 0)
        self.assertFalse(completion['request_count_complete'])
        self.assertTrue(completion['unknown_execution_tasks'])


if __name__ == '__main__':
    unittest.main()
