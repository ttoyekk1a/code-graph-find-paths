import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from benchmark50.runner.python_tool import DockerPython, PINNED_IMAGE_ID


class DockerPythonTests(unittest.TestCase):
    def setUp(self):
        self.runner = DockerPython(os.environ.get('BENCHMARK50_TEST_IMAGE', PINNED_IMAGE_ID),
                                   docker_host=os.environ.get('BENCHMARK50_DOCKER_HOST'),
                                   docker_config=os.environ.get('BENCHMARK50_DOCKER_CONFIG'))

    def test_launch_enforces_isolation_without_host_credentials_or_mounts(self):
        with patch.object(self.runner, 'docker', 'docker'), \
             patch.dict(os.environ, {'BENCHMARK50_SYNTHETIC_SECRET':'not-for-container'}), \
             patch('benchmark50.runner.python_tool.subprocess.Popen', side_effect=OSError('simulated launch failure')) as launch:
            result = self.runner.execute('print(question)', {'query_id':'public'}, [])
        self.assertEqual(result['status'], 'error')
        command = launch.call_args.args[0]
        for option, value in [('--network','none'), ('--cap-drop','ALL'),
                              ('--security-opt','no-new-privileges'), ('--user','65534:65534'),
                              ('--pull','never'), ('--memory','128m')]:
            self.assertEqual(command[command.index(option)+1], value)
        self.assertIn('--read-only', command)
        self.assertFalse(any(arg in command for arg in ['--volume', '-v', '--mount', '--env-file']))
        self.assertNotIn('BENCHMARK50_SYNTHETIC_SECRET', launch.call_args.kwargs['env'])
        self.assertEqual(launch.call_args.kwargs['cwd'], '/')

    def test_rejects_overlong_code_and_exhausted_deadline_before_launch(self):
        with patch.object(self.runner, "docker", "docker"), patch("benchmark50.runner.python_tool.subprocess.Popen") as launch:
            self.assertEqual(self.runner.execute("x" * 32001, "", [])["status"], "error")
            self.assertEqual(self.runner.execute("print(1)", "", [], 0)["status"], "timeout")
            launch.assert_not_called()

    @unittest.skipUnless(os.environ.get("BENCHMARK50_TEST_DOCKER") == "1", "Set BENCHMARK50_TEST_DOCKER=1 for real isolated Docker execution")
    def test_only_delivered_input_and_isolation(self):
        source = """
import os
from pathlib import Path
print(question, observations[0]['label'])
print('PYTHON_TOOL_TEST_CREDENTIAL' in os.environ)
print(sorted(os.listdir('/sys/class/net')))
print(Path(observations[0]['host_path']).exists())
print(os.getuid())
print(Path('/proc/self/status').read_text().split('CapEff:')[1].splitlines()[0].strip())
try:
    Path('/unexpected-write').write_text('blocked')
except OSError:
    print('root write denied')
Path('/tmp/marker').write_text('temporary')
"""
        with patch.dict(os.environ, {"PYTHON_TOOL_TEST_CREDENTIAL": "synthetic-secret"}):
            result = self.runner.execute(
                source, "Q?", [{"label": "delivered", "host_path": str(Path(__file__).resolve())}]
            )
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["cleanup"], "confirmed", result)
        lines = result["stdout"].splitlines()
        self.assertEqual(lines[:4], ["Q? delivered", "False", "['lo']", "False"])
        self.assertGreater(int(lines[4]), 0)
        self.assertEqual(lines[5:], ["0000000000000000", "root write denied"])
        result = self.runner.execute("import os; print(os.path.exists('/tmp/marker'))", "", [])
        self.assertEqual(result["stdout"], "False\n")

    @unittest.skipUnless(os.environ.get("BENCHMARK50_TEST_DOCKER") == "1", "Set BENCHMARK50_TEST_DOCKER=1 for real isolated Docker execution")
    def test_bounded_flood_and_timeout(self):
        flood_id, timeout_id = uuid.uuid4(), uuid.uuid4()
        with patch("benchmark50.runner.python_tool.uuid.uuid4", side_effect=[flood_id, timeout_id]):
            result = self.runner.execute("while True: print('z' * 10000)", "", [])
            self.assertEqual(result["status"], "output_limit", result["status"])
            self.assertEqual(result["cleanup"], "confirmed", result["cleanup"])
            self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 64000)
            result = self.runner.execute("while True: pass", "", [], remaining_seconds=2)
            self.assertEqual(result["status"], "timeout", result)
            self.assertEqual(result["cleanup"], "confirmed", result["cleanup"])
        for run_id in (flood_id, timeout_id):
            containers = subprocess.check_output(
                [self.runner.docker, "ps", "-aq", "--filter", f"name=^/all-paths-python-{run_id.hex}$"],
                text=True, env=self.runner.docker_env,
            )
            self.assertFalse(containers.strip(), containers)

    @unittest.skipUnless(os.environ.get("BENCHMARK50_TEST_DOCKER") == "1", "Set BENCHMARK50_TEST_DOCKER=1 for real isolated Docker execution")
    def test_serialized_output_budget_counts_escaping_and_metadata(self):
        result = self.runner.execute("print(chr(92) * 50000)", "", [])
        self.assertEqual(result["status"], "output_limit")
        self.assertTrue(result["stdout"].startswith(chr(92) * 100))
        self.assertLessEqual(len(json.dumps(result)), 64000)

    def test_oversized_input_rejected_before_launch(self):
        with patch.object(self.runner, "docker", "docker"), patch("benchmark50.runner.python_tool.subprocess.Popen") as launch:
            result = self.runner.execute("print(1)", "", ["x" * (2 * 1024 * 1024)])
            self.assertEqual(result["status"], "error")
            self.assertIn("input", result["error"].lower())
            launch.assert_not_called()

    @unittest.skipUnless(os.environ.get("BENCHMARK50_TEST_DOCKER") == "1", "Set BENCHMARK50_TEST_DOCKER=1 for real isolated Docker execution")
    def test_timeout_reserves_cleanup_budget_and_reports_unknown(self):
        def stalled_removal(*args, timeout, **kwargs):
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(args[0], timeout)

        with patch("benchmark50.runner.python_tool.subprocess.run", side_effect=stalled_removal):
            start = time.monotonic()
            result = self.runner.execute("while True: pass", "", [], remaining_seconds=2)
            elapsed = time.monotonic() - start
        try:
            self.assertEqual(result["status"], "timeout", result)
            self.assertLess(elapsed, 2.15)
            self.assertEqual(result["cleanup"], "unknown")
            self.assertRegex(result["container_name"], r"^all-paths-python-[0-9a-f]{32}$")
        finally:
            # The test deliberately simulated a failing daemon removal.
            name = result.get("container_name")
            if name:
                subprocess.run([self.runner.docker, "rm", "-f", name], capture_output=True, timeout=2, env=self.runner.docker_env)

    @unittest.skipUnless(os.environ.get("BENCHMARK50_TEST_DOCKER") == "1", "Set BENCHMARK50_TEST_DOCKER=1 for real isolated Docker execution")
    def test_python_failure_keeps_streams(self):
        result = self.runner.execute("print('before'); raise ValueError('test')", "", [])
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["stdout"], "before\n")
        self.assertIn("ValueError: test", result["stderr"])


if __name__ == "__main__":
    unittest.main()
