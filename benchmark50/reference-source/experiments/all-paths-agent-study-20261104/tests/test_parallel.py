from pathlib import Path
import json
import os
import signal
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_parallel import dispatch, schedule


STUB = """
import json, signal, sys, time
from pathlib import Path
p = Path(sys.argv[-1])
job = json.loads(p.read_text())
out = Path(job['output'])
out.mkdir()
(out / 'started').write_text(str(time.monotonic()))
if job['query_id'] == 'hold':
    def stop(*_):
        (out / 'relayed').touch()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    (out / 'ready').touch()
    time.sleep(10)
time.sleep(.2)
(out / 'ended').write_text(str(time.monotonic()))
(out / 'result.json').write_text(json.dumps({
    'query_id': job['query_id'], 'arm': job['arm'],
    'stop_reason': 'model_error' if job['query_id'] == 'fail' else 'submitted',
    'traces': []}))
"""


class ParallelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def run_jobs(self, *ids, **options):
        return dispatch(
            self.out, schedule([{'query_id': qid} for qid in ids]),
            [sys.executable, '-c', STUB], lambda: None, **options,
        )

    def test_wave_barrier_rotation_and_overlap(self):
        ledger, reason = self.run_jobs('q1', 'q2', 'q3')
        self.assertEqual(reason, 'completed')
        first = [item['arm'] for item in ledger[:3]]
        self.assertEqual([item['arm'] for item in ledger[3:6]], first[1:] + first[:1])
        self.assertEqual([item['arm'] for item in ledger[6:9]], first[2:] + first[:2])
        previous_end = 0
        for wave in range(0, 9, 3):
            dirs = [self.out / item['episode'] for item in ledger[wave:wave+3]]
            starts = [float((p / 'started').read_text()) for p in dirs]
            ends = [float((p / 'ended').read_text()) for p in dirs]
            self.assertGreater(min(starts), previous_end)
            self.assertLess(max(starts), min(ends))
            previous_end = max(ends)

    def test_model_error_drains_wave_and_stops_next_wave(self):
        ledger, reason = self.run_jobs('fail', 'q2')
        self.assertEqual(reason, 'model_error')
        self.assertEqual([r['state'] for r in ledger], ['finished'] * 3 + ['unstarted'] * 3)

    def test_existing_episode_rejected_before_launch(self):
        arm = schedule([{'query_id': 'q'}])[0][1]
        (self.out / f'q--{arm}').mkdir()
        with self.assertRaises(FileExistsError):
            self.run_jobs('q')

    def test_interrupt_relays_to_owned_workers(self):
        def interrupted():
            return signal.SIGTERM if len(list(self.out.glob('hold--*/ready'))) == 3 else None
        ledger, reason = self.run_jobs('hold', 'q2', interrupted=interrupted)
        self.assertEqual(reason, 'interrupted')
        self.assertEqual(len(list(self.out.glob('hold--*/relayed'))), 3)
        self.assertEqual([r['state'] for r in ledger[3:]], ['unstarted'] * 3)


if __name__ == '__main__':
    unittest.main()
