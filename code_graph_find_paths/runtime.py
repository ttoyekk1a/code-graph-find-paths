"""Standalone graph runtime and optional, explicit competition SDK backend."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
import uuid


def find_bundle() -> Path:
    """Find the original frozen bundle without silently ignoring an override."""
    override = os.environ.get('CODE_GRAPH_BUNDLE')
    candidates = ([Path(override).expanduser()] if override else [
        Path(__file__).resolve().parent / '_frozen_bundle',
        Path(__file__).resolve().parents[1] / 'graph-find-paths',
    ])
    for candidate in candidates:
        if (candidate / 'scripts/build_tool.py').is_file() and (candidate / 'references/tool-schema.json').is_file():
            return candidate.resolve()
    raise FileNotFoundError('Frozen graph-find-paths bundle not found; set CODE_GRAPH_BUNDLE to its directory')


def get_tool_schema() -> dict[str, Any]:
    """Return the exact frozen OpenAI function declaration, with no SDK import."""
    return json.loads((find_bundle() / 'references/tool-schema.json').read_text(encoding='utf-8'))


def _load_binding(bundle: Path):
    spec = importlib.util.spec_from_file_location('_code_graph_find_paths_binding', bundle / 'scripts/build_tool.py')
    if spec is None or spec.loader is None:
        raise ImportError('Cannot load frozen graph tool builder')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompetitionRuntime:
    """Own an official SDK workspace and one budgeted frozen graph session.

    Use ``with GraphRuntime(path, sha256) as runtime: runtime.query(**args)``.
    ``attempts`` counts all query dispatches, including malformed arguments and
    rejected calls. The host limits all attempts to ``tool_calls``; admitted
    native calls also retain the frozen SDK gate unchanged.
    A closed instance cannot restart; create another instance for a new session.
    """

    def __init__(self, graph_path: str | Path, expected_graph_sha256: str, *,
                 tool_calls: int = 8, task_id: str = 'local-graph',
                 work_dir: str | Path | None = None):
        if type(tool_calls) is not int or tool_calls < 1:
            raise ValueError('tool_calls must be a positive integer')
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError('task_id must be a nonempty string')
        if not isinstance(expected_graph_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_graph_sha256):
            raise ValueError('expected_graph_sha256 must be 64 lowercase hexadecimal characters')
        self.graph_path = Path(graph_path).expanduser().resolve()
        self.expected_graph_sha256 = expected_graph_sha256
        self.tool_calls = tool_calls
        self.task_id = task_id
        self.work_dir = Path(work_dir).expanduser().resolve() if work_dir is not None else None
        self.attempts = 0
        self._attempt_log: list[dict[str, Any]] = []
        self._schema = get_tool_schema()
        self._lock = threading.RLock()
        self._state = 'new'
        self._temp = None
        self._manager = None
        self._session = None
        self._registry = None
        self.context = None
        self.ownership = None
        self.workspace_path: Path | None = None

    @property
    def schema(self) -> dict[str, Any]:
        return deepcopy(self._schema)

    @property
    def attempt_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._attempt_log)

    @property
    def closed(self) -> bool:
        return self._state == 'closed'

    def start(self) -> 'CompetitionRuntime':
        with self._lock:
            if self._state == 'running':
                return self
            if self._state != 'new':
                raise RuntimeError('This GraphRuntime is closed; create a new instance')
            try:
                if hashlib.sha256(self.graph_path.read_bytes()).hexdigest() != self.expected_graph_sha256:
                    raise ValueError('SHA256Mismatch: graph bytes do not match expected_graph_sha256')
                # Import only real installed SDK modules, never synthetic stand-ins.
                from swegemma.context import SwegemmaContext
                from swegemma.sandbox import SubprocessManager
                from adk_submission.registry import ToolRegistry
                binding = _load_binding(find_bundle())
                from decision_lab.graph_native_v1.source_read import live_identity
                self._temp = tempfile.TemporaryDirectory(prefix='code_graph_runtime_', dir=self.work_dir)
                self._manager = SubprocessManager(base_dir=Path(self._temp.name), system_site_packages=False)
                sid = self._manager.start()
                paths = self._manager.sandboxes[sid]
                self.workspace_path = paths['workspace'].resolve()
                self.context = SwegemmaContext(docker_manager=self._manager, container_id=sid,
                                               task_id=self.task_id, max_tool_calls=self.tool_calls)
                probe = self.context.exec("python3 -I -B -c 'import os; print(os.getcwd())'")
                if (probe.exit_code != 0 or Path(probe.stdout.strip()).resolve() != self.workspace_path
                        or self.context.sandbox.sandbox_id != sid or not paths['root'].is_dir()):
                    raise RuntimeError('Official SDK workspace ownership probe failed')
                self.ownership = {'identity': live_identity(self.context, '/workspace'), 'ownership': {
                    'attested_owned': True, 'owner': f'code_graph_find_paths:pid={os.getpid()}',
                    'record_id': str(uuid.uuid4()), 'task_id': self.task_id,
                    'container_id': sid, 'workspace_root': '/workspace'}, 'evidence': {
                    'created_by_this_process': True, 'actual_workspace': str(self.workspace_path),
                    'exec_probe': probe.stdout.strip(), 'source_snapshot_loaded': False}}
                tool, self._session = binding.build_graph_find_paths(
                    self.context, graph_path=self.graph_path,
                    expected_graph_sha256=self.expected_graph_sha256, runtime_identity=self.ownership)
                if tool.__name__ != 'graph_find_paths' or binding.TOOL_SCHEMA != self._schema:
                    raise RuntimeError('Frozen tool declaration mismatch')
                self._registry = ToolRegistry()
                self._registry.register('graph_find_paths', tool)
                self._state = 'running'
                return self
            except BaseException:
                self.close()
                raise

    def query(self, **arguments: Any) -> dict[str, Any]:
        """Dispatch one call unchanged and return its full original evidence packet."""
        return self.dispatch('graph_find_paths', arguments)

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Count every attempt, including unknown names and malformed arguments."""
        with self._lock:
            self.attempts += 1
            record: dict[str, Any] = {'attempt': self.attempts, 'name': name}
            self._attempt_log.append(record)
            try:
                if self._state != 'running':
                    raise RuntimeError('GraphRuntime is not running; use it as a context manager')
                def controller_error(error_type):
                    packet = {'status': 'error', 'error_type': error_type, 'scope': 'controller'}
                    record.update(packet)
                    return packet
                if self.attempts > self.tool_calls:
                    return controller_error('ControllerAttemptBudgetExceeded')
                if not isinstance(name, str) or name not in self._registry:
                    return controller_error('UnknownTool')
                if type(arguments) is not dict:
                    return controller_error('InvalidArguments')
                tool = self._registry.get(name)
                try:
                    inspect.signature(tool).bind(**arguments)
                except TypeError:
                    return controller_error('InvalidArguments')
                result = tool(**arguments)
                record['status'] = result.get('status')
                record['error_type'] = result.get('error_type')
                return result
            except BaseException as exc:
                record['exception'] = type(exc).__name__
                raise

    def close(self) -> None:
        """Close the graph session and delete only this instance's owned workspace."""
        with self._lock:
            if self._state == 'closed':
                return
            self._state = 'closed'
            try:
                if self._session is not None:
                    self._session.close()
            finally:
                try:
                    if self._manager is not None:
                        self._manager.close()
                finally:
                    if self._temp is not None:
                        self._temp.cleanup()

    def __enter__(self) -> 'GraphRuntime':
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _LocalContext:
    """Actual in-process budget and lifecycle; not a competition SDK stand-in."""
    def __init__(self, tool_calls, task_id):
        from types import SimpleNamespace
        self.tool_lock = threading.RLock()
        self.tool_calls_used = 0
        self.budget = SimpleNamespace(tool_calls=tool_calls)
        self.task_id = task_id
        self.identity = uuid.uuid4().hex
        self.closed = False

    def check_budget(self, *, count_tool_call=True):
        if self.closed or self.tool_calls_used >= self.budget.tool_calls:
            return json.dumps({'status': 'error', 'error_type': 'BudgetExceeded'})
        self.tool_calls_used += int(count_tool_call)
        return None


class GraphRuntime(CompetitionRuntime):
    """Standalone immutable graph session, requiring no SDK, network or sandbox.

    A missing expected hash pins the bytes supplied by the caller at construction;
    it does not establish external provenance. Use CompetitionRuntime explicitly
    when reproducing the original SDK integration.
    """
    def __init__(self, graph_path, expected_graph_sha256=None, **kwargs):
        path = Path(graph_path).expanduser().resolve()
        if expected_graph_sha256 is None:
            # Bound allocation before the engine's own strict loader runs.
            from ._engine.core import GraphWitnessSession
            with path.open('rb') as stream:
                raw = stream.read(GraphWitnessSession.MAX_FILE_BYTES + 1)
            if len(raw) > GraphWitnessSession.MAX_FILE_BYTES:
                raise ValueError('FileCapExceeded')
            expected_graph_sha256 = hashlib.sha256(raw).hexdigest()
        super().__init__(path, expected_graph_sha256, **kwargs)

    def start(self):
        with self._lock:
            if self._state == 'running':
                return self
            if self._state != 'new':
                raise RuntimeError('This GraphRuntime is closed; create a new instance')
            try:
                from ._engine.session import LeanGraphWitnessSession, make_graph_find_paths
                from ._engine.search import TargetedCore
                self.context = _LocalContext(self.tool_calls, self.task_id)
                self._session = LeanGraphWitnessSession(
                    self.context, graph_path=self.graph_path,
                    expected_graph_sha256=self.expected_graph_sha256,
                    output_limit=8000, core_output_limit=6000)
                self._session.core = TargetedCore.from_loaded(
                    self._session.core, mode='bidirectional', max_nodes=160, max_rows=800)
                tool = make_graph_find_paths(self._session)
                tool.__doc__ = self._schema['function']['description']
                self._registry = {'graph_find_paths': tool}
                self._state = 'running'
                return self
            except BaseException:
                self.close()
                raise

    def close(self):
        with self._lock:
            try:
                super().close()
            finally:
                if self.context is not None:
                    self.context.closed = True
