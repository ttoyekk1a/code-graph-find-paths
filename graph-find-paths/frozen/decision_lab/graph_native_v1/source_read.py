"""Bounded fd-relative byte capture. Runtime transport never reads host files.

The fixed library below is also the complete audited reader shipped by value to
python3 -I -B -c. It imports stdlib only and never imports repository modules.
Its stdout emission is capped; stderr is accepted/rejected only AFTER ctx.exec
returns. This API provides no upstream buffering bound or clipping signal.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
from time import monotonic, perf_counter

STDOUT_CAP = 1500000
STDERR_CAP = 4096  # Post-return acceptance/exposure limit, not capture size.
HARD_LIMITS = dict(max_requested_files=16, max_open_files=8,
                   max_file_bytes=262144, max_total_bytes=1048576)

SAFE_LIBRARY = r'''
import os, stat, time, hashlib, base64, json

def valid_path(path):
    return (type(path) is str and path.endswith('.py') and
        not any(c in path for c in ('\\', '\x00', ':')) and
        all(p not in ('', '.', '..', 'tests', 'verifier', '.git') for p in path.split('/')))

def identity(s):
    return {k: getattr(s, 'st_' + k) for k in ('dev','ino','size','mtime_ns','ctime_ns')}

def open_root(root):
    if type(root) is not str or not root.startswith('/'):
        raise ValueError('UnsafePath')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.split('/')[1:]:
            if not part or part in ('.', '..'):
                raise ValueError('UnsafePath')
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise

def open_leaf(rootfd, path):
    directory = os.dup(rootfd)
    try:
        pieces = path.split('/')
        for part in pieces[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = new
        return os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)

def stat_leaf(rootfd, path):
    directory = os.dup(rootfd)
    try:
        pieces = path.split('/')
        for part in pieces[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = new
        return os.stat(pieces[-1], dir_fd=directory, follow_symlinks=False)
    finally:
        os.close(directory)

def capture(root, paths, limits):
    hard = dict(max_requested_files=16, max_open_files=8, max_file_bytes=262144, max_total_bytes=1048576)
    cap = {k: min(v, limits.get(k,v)) for k,v in hard.items()}
    if any(type(v) is not int or v < 0 for v in cap.values()):
        raise ValueError('InvalidSourceLimits')
    deadline = time.monotonic() + min(10.0, max(0.0, limits.get('timeout_seconds',10.0)))
    work = dict(requested_files=len(paths), distinct_files=len(set(paths)), opened_files=0,
                bytes_read=0, bytes_hashed=0, bytes_transferred=0, runtime_exec_operations=0,
                incomplete=False)
    records = {}
    rootfd = None
    try:
        rootfd = open_root(root)
        root_identity = identity(os.fstat(rootfd))
        for path in dict.fromkeys(paths):
            record = dict(relative_path=path, status='SourceBudgetExceeded')
            records[path] = record
            if not valid_path(path):
                record['status'] = 'UnsafePath'
                continue
            if len(records) > cap['max_requested_files'] or work['opened_files'] >= cap['max_open_files']:
                continue
            if time.monotonic() >= deadline:
                record['status'] = 'SourceTimeout'
                continue
            fd = None
            try:
                fd = open_leaf(rootfd, path)
                work['opened_files'] += 1
                before = os.fstat(fd)
                record['stat_before'] = identity(before)
                if not stat.S_ISREG(before.st_mode):
                    record['status'] = 'NotRegularFile'
                    continue
                if before.st_nlink != 1:
                    record['status'] = 'HardlinkRejected'
                    continue
                if before.st_size > cap['max_file_bytes']:
                    record['status'] = 'SourceTooLarge'
                    continue
                if before.st_size > cap['max_total_bytes'] - work['bytes_read']:
                    continue
                chunks = []
                size = 0
                while True:
                    if time.monotonic() >= deadline:
                        record['status'] = 'SourceTimeout'
                        break
                    remaining = cap['max_total_bytes'] - work['bytes_read']
                    if remaining == 0:
                        # fstat size alone cannot establish EOF against growth.
                        record['status'] = 'SourceBudgetExceeded'
                        break
                    block = os.read(fd, min(65536, remaining, cap['max_file_bytes'] + 1 - size))
                    size += len(block)
                    work['bytes_read'] += len(block)
                    chunks.append(block)
                    if size > cap['max_file_bytes']:
                        record['status'] = 'SourceTooLarge'
                        break
                    if not block:
                        raw = b''.join(chunks)
                        record.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                        work['bytes_hashed'] += len(raw)
                        after = os.fstat(fd)
                        record['stat_after'] = identity(after)
                        checkroot = open_root(root)
                        try:
                            path_after = stat_leaf(checkroot, path)
                            record['path_stat_after'] = identity(path_after)
                            if (identity(before) != identity(after) or
                                identity(after) != identity(path_after) or
                                identity(os.fstat(checkroot)) != root_identity or path_after.st_nlink != 1 or not stat.S_ISREG(path_after.st_mode)):
                                record['status'] = 'SourceRace'
                            else:
                                record.update(status='ok', data_b64=base64.b64encode(raw).decode('ascii'))
                        except (OSError, ValueError):
                            record['status'] = 'SourceRace'
                        finally:
                            os.close(checkroot)
                        break
            except (OSError, ValueError):
                record['status'] = 'UnsafePath'
            finally:
                if fd is not None: os.close(fd)
    except (OSError, ValueError):
        for path in dict.fromkeys(paths): records[path] = dict(relative_path=path, status='UnsafePath')
    finally:
        if rootfd is not None: os.close(rootfd)
    return dict(records=records, work=work)
'''
exec(compile(SAFE_LIBRARY, '<fixed-bounded-reader>', 'exec'))
RUNTIME_PROGRAM = SAFE_LIBRARY + r'''
import sys
try:
    request = json.loads(sys.argv[1])
    result = capture(request['root'], request['paths'], request['limits'])
    output = json.dumps(result, separators=(',', ':'), ensure_ascii=True)
    if len(output.encode('utf-8')) > 1500000:
        result = dict(error='SourceTransportOversize', work=result['work'], records={})
        output = json.dumps(result, separators=(',', ':'))
    sys.stdout.write(output)
except Exception:
    sys.stdout.write('{"error":"SourceReadFailure","records":{},"work":{"incomplete":true}}')
'''


def _limits(limits):
    if type(limits) is not dict:
        raise ValueError('InvalidSourceLimits')
    result = {}
    for key, maximum in HARD_LIMITS.items():
        value = limits.get(key, maximum)
        if type(value) is not int or value < 0:
            raise ValueError('InvalidSourceLimits')
        result[key] = min(value, maximum)
    if 'timeout_seconds' in limits:
        value = limits['timeout_seconds']
        if type(value) not in (int, float) or not 0 <= value <= 10:
            raise ValueError('InvalidSourceLimits')
        result['timeout_seconds'] = value
    return result


def _paths(paths):
    if type(paths) is not list or len(paths) > 40 or any(type(p) is not str for p in paths):
        raise ValueError('InvalidSourcePaths')
    return list(dict.fromkeys(paths))


def _strict_pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError('DuplicateTransportKey')
        result[k] = v
    return result


def decode_capture(result, paths, limits):
    """Validate untrusted runtime bytes before exposing any captured source."""
    if type(result) is not dict or result.get('error') or type(result.get('records')) is not dict:
        raise ValueError('SourceReadFailure')
    records, work = result['records'], result.get('work')
    if set(records) != set(paths) or type(work) is not dict or work.get('incomplete') is not False:
        raise ValueError('MalformedSourceTransport')
    for key in ('requested_files', 'distinct_files', 'opened_files', 'bytes_read', 'bytes_hashed', 'bytes_transferred', 'runtime_exec_operations'):
        if type(work.get(key)) is not int or work[key] < 0:
            raise ValueError('MalformedSourceCounters')
    if (work['opened_files'] > limits['max_open_files'] or work['bytes_read'] > limits['max_total_bytes']
            or work['bytes_hashed'] > work['bytes_read'] or work['distinct_files'] != len(paths)):
        raise ValueError('MalformedSourceCounters')
    total = 0
    for path, record in records.items():
        if type(record) is not dict or record.get('relative_path') != path or type(record.get('status')) is not str:
            raise ValueError('MalformedSourceRecord')
        if record['status'] == 'ok':
            raw = base64.b64decode(record.pop('data_b64'), validate=True)
            if len(raw) > limits['max_file_bytes'] or len(raw) != record.get('size') or hashlib.sha256(raw).hexdigest() != record.get('sha256'):
                raise ValueError('SourceTransportHashMismatch')
            for name in ('stat_before', 'stat_after', 'path_stat_after'):
                stat = record.get(name)
                if type(stat) is not dict or set(stat) != {'dev','ino','size','mtime_ns','ctime_ns'} or any(type(v) is not int for v in stat.values()):
                    raise ValueError('MalformedSourceStat')
            if record['stat_before'] != record['stat_after'] or record['stat_after'] != record['path_stat_after'] or record['stat_after']['size'] != len(raw):
                raise ValueError('SourceTransportRace')
            record['raw_bytes'] = raw
            total += len(raw)
        elif 'data_b64' in record or 'raw_bytes' in record:
            raise ValueError('UnexpectedSourceData')
    if total > work['bytes_read'] or total > work['bytes_hashed']:
        raise ValueError('MalformedSourceCounters')
    return result


class HostSnapshotReader:
    """Engineering proof only, never accepted by a native factory."""
    host_proof_only = True

    def __init__(self, source_root: Path, *, provenance: dict):
        self.source_root = str(source_root.absolute())
        self.provenance = copy.deepcopy(provenance)

    def read_files(self, relative_paths: list[str], *, limits: dict) -> dict:
        paths = _paths(relative_paths)
        cap = _limits(limits)
        result = decode_capture(capture(self.source_root, paths, cap), paths, cap)
        result['work']['requested_files'] = len(relative_paths)
        result['provenance'] = dict(mode='host_proof_only', **self.provenance)
        return result


def live_identity(ctx, workspace_root):
    sandbox = ctx.sandbox
    return dict(task_id=ctx.task_id, container_id=ctx.container_id,
                sandbox_object_id=id(sandbox), sandbox_id=getattr(sandbox, 'sandbox_id', None),
                sandbox_work_dir=getattr(sandbox, 'work_dir', None), workspace_root=workspace_root)


def validate_runtime(ctx, workspace_root, supplied):
    if type(supplied) is not dict or supplied.get('identity') != live_identity(ctx, workspace_root):
        raise ValueError('RuntimeIdentityMismatch')
    ownership = supplied.get('ownership')
    if (type(ownership) is not dict or ownership.get('attested_owned') is not True
            or not isinstance(ownership.get('owner'), str) or not ownership['owner']
            or not isinstance(ownership.get('record_id'), str) or not ownership['record_id']
            or ownership.get('task_id') != ctx.task_id or ownership.get('container_id') != ctx.container_id
            or ownership.get('workspace_root') != workspace_root or ctx.sandbox is None or not ctx.container_id):
        raise ValueError('MissingRuntimeOwnership')
    if not isinstance(workspace_root, str) or not workspace_root.startswith('/') or any(p in ('','.', '..') for p in workspace_root.split('/')[1:]):
        raise ValueError('InvalidWorkspaceRoot')
    return copy.deepcopy(supplied)


class RuntimeSnapshotReader:
    """Only the active context executes the fixed fd-relative byte reader."""
    host_proof_only = False

    def __init__(self, ctx, *, workspace_root: str, runtime_identity: dict):
        self.ctx = ctx
        self.workspace_root = workspace_root
        self.runtime_identity = validate_runtime(ctx, workspace_root, runtime_identity)
        self._initial = live_identity(ctx, workspace_root)
        self._poisoned = False

    def check_identity(self):
        if self._poisoned or live_identity(self.ctx, self.workspace_root) != self._initial:
            self._poisoned = True
            raise ValueError('RuntimeChanged')

    def read_files(self, relative_paths: list[str], *, limits: dict) -> dict:
        paths = _paths(relative_paths)
        cap = _limits(limits)
        timeouts = [10.0, cap.get('timeout_seconds', 10.0)]
        remaining = self.ctx.remaining_time_seconds
        if remaining is not None: timeouts.append(remaining)
        configured = self.ctx.harness.command_timeout_seconds
        if configured is not None: timeouts.append(configured)
        timeout = min(timeouts)
        work = dict(runtime_exec_operations=0, requested_files=len(paths), distinct_files=len(paths),
                    opened_files=None, bytes_read=None, bytes_hashed=None, bytes_transferred=0, stderr_bytes_returned=None, incomplete=True)
        transport = dict(stdout_emission_limit_bytes=STDOUT_CAP, stderr_acceptance_limit_bytes=STDERR_CAP,
                         stderr_limit_scope='post_return_acceptance', stdout_bytes_returned=None,
                         stderr_bytes_returned=None, upstream_capture_limit_bytes=None, upstream_clipped=None)
        def failed(status):
            return dict(error=status, records={p:dict(relative_path=p,status=status) for p in paths}, work=work, transport=transport)
        try:self.check_identity()
        except ValueError:
            work.update(opened_files=0,bytes_read=0,bytes_hashed=0,incomplete=False)
            return failed('RuntimeChanged')
        if timeout <= 0:
            work.update(opened_files=0,bytes_read=0,bytes_hashed=0,incomplete=False)
            return failed('SourceTimeout')
        cap['timeout_seconds'] = timeout
        request = json.dumps(dict(root=self.workspace_root, paths=paths, limits=cap), separators=(',', ':'))
        command = 'python3 -I -B -c ' + shlex.quote(RUNTIME_PROGRAM) + ' ' + shlex.quote(request)
        work['runtime_exec_operations'] = 1
        started = perf_counter()
        try:
            response = self.ctx.exec(command, timeout=timeout)
            self.check_identity()
            stdout = response.stdout
            stderr = response.stderr
            if type(stdout) is not str or type(stderr) is not str:
                return failed('MalformedSourceTransport')
            work['bytes_transferred'] = len(stdout.encode('utf-8'))
            work['stderr_bytes_returned'] = len(stderr.encode('utf-8'))
            transport.update(stdout_bytes_returned=work['bytes_transferred'], stderr_bytes_returned=work['stderr_bytes_returned'])
            if work['bytes_transferred'] > STDOUT_CAP or work['stderr_bytes_returned'] > STDERR_CAP:
                return failed('SourceTransportOversize')
            if getattr(response, 'status', None) == 'timeout' or getattr(response, 'timed_out', False):
                return failed('SourceTimeout')
            if response.exit_code != 0:
                return failed('SourceReadFailure')
            result = json.loads(stdout, object_pairs_hook=_strict_pairs,
                                parse_constant=lambda s: (_ for _ in ()).throw(ValueError('NonJSON')))
            if type(result) is dict and result.get('error') == 'SourceTransportOversize' and result.get('records') == {}:
                # The helper finished counting but omitted its oversized payload.
                # Validate those bounded counters; do not call completed work zero.
                result = dict(records={p:dict(relative_path=p,status='SourceTransportOversize') for p in paths},work=result.get('work'))
            result = decode_capture(result, paths, cap)
            result['work'].update(runtime_exec_operations=1, requested_files=len(relative_paths), bytes_transferred=work['bytes_transferred'],stderr_bytes_returned=work['stderr_bytes_returned'])
            result['transport'] = transport
            result['provenance'] = dict(mode='actual_runtime_capture', identity_before=self._initial,
                                       identity_after=live_identity(self.ctx,self.workspace_root))
            return result
        except ValueError as exc:
            return failed('RuntimeChanged' if str(exc) == 'RuntimeChanged' else 'MalformedSourceTransport')
        except Exception:
            return failed('SourceReadFailure')
        finally:
            work['elapsed_seconds'] = perf_counter() - started
