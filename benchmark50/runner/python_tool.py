"""Run agent Python against explicitly supplied observations in a disposable container."""

import json
import os
import re
import selectors
import shutil
import subprocess
import time
import tempfile
import uuid


PINNED_IMAGE_ID = "sha256:38758a82a44d1acb9bae3dd5f7d2a55452fb44a5ceca7c4589f360f2c4aa3d0c"
_MAX_INPUT_BYTES = 1024 * 1024
_BOOTSTRAP = """
import json
import sys

payload = json.load(sys.stdin)
exec(compile(payload["code"], "<agent_python>", "exec"), {
    "__name__": "__main__",
    "question": payload["question"],
    "observations": payload["observations"],
})
"""
_DOCKER_ENV = {"PATH": os.defpath, "DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONFIG": "/dev/null"}


def fit_output_prefix(result, max_chars):
    """Retain the longest stdout/stderr prefix within the serialized JSON budget."""
    output = result.get("stdout", "") + result.get("stderr", "")
    stdout_length = len(result.get("stdout", ""))
    result["stdout"] = ""
    result["stderr"] = ""
    remaining = max_chars - len(json.dumps(result))
    # JSON escaping costs are additive once the empty-string quotes are counted.
    low, high = 0, len(output)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(output[:middle])) - 2 <= remaining:
            low = middle
        else:
            high = middle - 1
    result["stdout"] = output[:min(low, stdout_length)]
    result["stderr"] = output[stdout_length:low]
    return result


class DockerPython:
    def __init__(self, image_id, timeout_seconds=10, max_output_chars=64000, *, docker_host=None, docker_config=None):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("Python execution requires a full local Docker image ID")
        if not 0 < timeout_seconds <= 10 or not 256 <= max_output_chars <= 64000:
            raise ValueError("Python execution limits exceed the supported range")
        self.image_id = image_id
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.docker = shutil.which("docker")
        self.docker_env = {**_DOCKER_ENV, "PATH": os.environ.get("PATH", os.defpath)}
        self.docker_env["DOCKER_HOST"] = docker_host or os.environ.get("DOCKER_HOST", _DOCKER_ENV["DOCKER_HOST"])
        self._empty_docker_config = None
        if docker_config is None:
            self._empty_docker_config = tempfile.TemporaryDirectory(prefix="graph-python-config-")
            docker_config = self._empty_docker_config.name
        self.docker_env["DOCKER_CONFIG"] = str(docker_config)

    def close(self):
        config = getattr(self, '_empty_docker_config', None)
        if config is not None:
            config.cleanup()
            self._empty_docker_config = None

    def __del__(self):
        self.close()

    def execute(self, code, question, observations, remaining_seconds=600):
        deadline = time.monotonic() + min(self.timeout_seconds, remaining_seconds)
        result = {"status": "error", "stdout": "", "stderr": "", "cleanup": "not_started"}
        if not isinstance(code, str) or len(code) > 32000:
            return {**result, "error": "code must be a string of at most 32000 characters"}
        if deadline <= time.monotonic():
            return {**result, "status": "timeout"}
        if self.docker is None:
            return {**result, "error": "Docker CLI unavailable"}

        # Reserve part of the same wall clock budget for cleanup and response sizing.
        remaining = deadline - time.monotonic()
        cleanup_deadline = deadline - min(0.03, remaining * 0.1)
        run_deadline = cleanup_deadline - min(0.75, remaining * 0.4)
        try:
            payload = self._encode_input(
                {"code": code, "question": question, "observations": observations}, run_deadline
            )
        except TimeoutError:
            return {**result, "status": "timeout"}
        except (TypeError, ValueError, RecursionError) as error:
            return {**result, "error": str(error)}

        if run_deadline <= time.monotonic():
            return {**result, "status": "timeout"}

        name = f"all-paths-python-{uuid.uuid4().hex}"
        command = [
            self.docker, "run", "--rm", "--name", name, "--interactive",
            "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "32",
            "--memory", "128m", "--memory-swap", "128m", "--cpus", "1",
            "--user", "65534:65534", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--workdir", "/tmp", "--log-driver", "none", "--entrypoint", "/usr/local/bin/python",
            self.image_id, "-I", "-B", "-u", "-c", _BOOTSTRAP,
        ]
        process = None
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.docker_env, cwd="/",
            )
            result["status"] = self._exchange(process, payload, streams, run_deadline)
        except (OSError, subprocess.SubprocessError):
            result["error"] = "Docker execution failed"
        finally:
            if process is not None:
                returncode = process.poll()
                try:
                    if returncode is None:
                        process.kill()
                        try:
                            process.wait(timeout=max(0, min(0.1, cleanup_deadline - time.monotonic())))
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
                    run_completed = returncode is not None and 0 <= returncode < 125
                    if self._remove(name, cleanup_deadline, run_completed):
                        result["cleanup"] = "confirmed"
                    else:
                        result["cleanup"] = "unknown"
                        result["container_name"] = name
        for stream, output in streams.items():
            result[stream] = output.decode("utf-8", errors="replace")
        return self._fit_result(result)

    @staticmethod
    def _encode_input(value, deadline):
        # Precheck scalar sizes before JSONEncoder can allocate escaped strings.
        # Iterators retain only O(depth) entries while traversing a wide array.
        remaining = _MAX_INPUT_BYTES

        def check(item, depth=0):
            nonlocal remaining
            if time.monotonic() >= deadline:
                raise TimeoutError
            if depth > 64:
                raise ValueError("Python tool input exceeds maximum JSON depth")
            item_type = type(item)
            remaining -= max(1, len(item)) if item_type is str else 1
            if remaining < 0:
                raise ValueError("Python tool input exceeds 1 MiB JSON limit")
            if item_type is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise TypeError("Python tool input requires string object keys")
                    check(key, depth + 1)
                    check(child, depth + 1)
            elif item_type in (list, tuple):
                for child in item:
                    check(child, depth + 1)
            elif item_type is int and item.bit_length() > 1024:
                raise ValueError("Python tool input integer exceeds 1024 bits")
            elif item_type not in (str, int, float, bool, type(None)):
                raise TypeError("Python tool input requires JSON-compatible values")

        check(value)
        payload = bytearray()
        for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(value):
            if time.monotonic() >= deadline:
                raise TimeoutError
            if len(payload) + len(chunk) > _MAX_INPUT_BYTES:
                raise ValueError("Python tool input exceeds 1 MiB JSON limit")
            payload.extend(chunk.encode("ascii"))
        return payload

    def _fit_result(self, result):
        if len(json.dumps(result)) <= self.max_output_chars:
            return result

        result["original_status"] = result["status"]
        result["output_truncated"] = True
        result["status"] = "output_limit"
        return fit_output_prefix(result, self.max_output_chars)

    def _exchange(self, process, payload, streams, deadline):
        remaining_output = self.max_output_chars
        pending = memoryview(payload)
        with selectors.DefaultSelector() as selector:
            for stream in streams:
                pipe = getattr(process, stream)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, stream)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                for event, _ in selector.select(remaining):
                    pipe = event.fileobj
                    if event.data == "stdin":
                        try:
                            pending = pending[os.write(pipe.fileno(), pending[:16384]):]
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            pending = pending[len(pending):]
                        if not pending:
                            selector.unregister(pipe)
                            pipe.close()
                        continue
                    try:
                        chunk = os.read(pipe.fileno(), min(16384, remaining_output + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(pipe)
                        continue
                    streams[event.data].extend(chunk[:remaining_output])
                    if len(chunk) > remaining_output:
                        return "output_limit"
                    remaining_output -= len(chunk)

        try:
            exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return "timeout"
        return "ok" if exit_code == 0 else "error"

    def _remove(self, name, deadline, run_completed):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            removed = subprocess.run(
                [self.docker, "rm", "--force", name],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self.docker_env, cwd="/", timeout=remaining, check=False,
            )
            if removed.returncode == 0:
                return True
            # The daemon may already have applied --rm. Only accept absence after
            # the run CLI has exited normally to avoid racing a pending create.
            remaining = deadline - time.monotonic()
            if not run_completed or remaining <= 0:
                return False
            listing = subprocess.run(
                [self.docker, "ps", "-aq", "--filter", f"name=^/{name}$"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=self.docker_env, cwd="/", timeout=remaining, check=False,
            )
            return listing.returncode == 0 and not listing.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return False
