"""Explicitly enabled three-worker campaign. Each question is one wave."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True

from python_tool import DockerPython
from run_agents import evaluate, launch_gate, run_path, schedule, summarize
from support import CONFIG, HERE, digest, freeze_files, load_inputs, save


STOP_REASONS = {'model_error', 'runner_error', 'interrupted', 'python_cleanup_unconfirmed'}
CLEANUP_SECONDS = 12


def now():
    return datetime.now(timezone.utc).isoformat()


def read_result(out, record):
    row = json.loads((out / record['episode'] / 'result.json').read_text())
    if (row['query_id'], row['arm']) != (record['query_id'], record['arm']):
        raise ValueError('Episode result does not match its job')
    return row


def outcome(out, record):
    try:
        row = read_result(out, record)
        if any(isinstance(t.get('raw_result'), dict) and t['raw_result'].get('cleanup') == 'unknown'
               for t in row['traces']):
            return 'python_cleanup_unconfirmed'
        if row['stop_reason'] in STOP_REASONS:
            return row['stop_reason']
    except FileNotFoundError:
        return 'child_nonzero' if record['returncode'] else 'missing_result'
    except (AttributeError, ValueError, TypeError, KeyError, OSError):
        return 'invalid_result'
    return 'child_nonzero' if record['returncode'] else None


def signal_groups(active, signum):
    for _, process, _ in active:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass


def dispatch(out, jobs, worker_command, verify_inputs, *, interrupted=lambda: None,
             wave_finished=lambda _: None, worker_context=None):
    out = out.resolve()
    ledger = []
    for question, arm in jobs:
        qid = question['query_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', qid) or arm not in CONFIG['arms']:
            raise ValueError('Unsafe episode identifier')
        ledger.append({'query_id': qid, 'arm': arm, 'episode': f'{qid}--{arm}', 'state': 'unstarted'})
    names = {r['episode'] for r in ledger}
    if len(names) != len(ledger) or any((out / n).exists() or (out / n).is_symlink() for n in names):
        raise FileExistsError('Duplicate episode output; no overwrite/resume/retry')
    ledger_path = out / 'ledger.json'
    dispatch_dir = out / 'jobs'
    dispatch_dir.mkdir(exist_ok=False)

    def persist():
        save(ledger_path, ledger)

    persist()
    for start in range(0, len(jobs), 3):
        if interrupted():
            return ledger, 'interrupted'
        verify_inputs()
        active = []
        reason = None
        try:
            for index in range(start, min(start + 3, len(jobs))):
                if interrupted():
                    break
                record = ledger[index]
                job_path = dispatch_dir / f'{index:03d}.json'
                question, arm = jobs[index]
                save(job_path, {**(worker_context or {}), 'query_id': record['query_id'],
                                'arm': arm, 'question': question, 'output': str(out / record['episode'])})
                command = [*worker_command, str(job_path)]
                record.update(state='attempted', command=command, job=str(job_path), attempted_at=now())
                persist()
                with job_path.with_suffix('.log').open('xb') as log:
                    try:
                        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                                   stderr=subprocess.STDOUT, start_new_session=True)
                    except OSError as exc:
                        record.update(launch_error=type(exc).__name__, stop_reason='child_launch_error')
                        reason = 'child_launch_error'
                        persist()
                        break
                    deadline = time.monotonic() + CONFIG['max_episode_seconds']
                    active.append((record, process, deadline))
                    record.update(state='started', pid=process.pid, process_group=process.pid, started_at=now())
                    persist()

            cleanup_deadline = None
            kill_deadline = None
            while active:
                now_monotonic = time.monotonic()
                for item in active[:]:
                    record, process, deadline = item
                    returncode = process.poll()
                    if returncode is None:
                        continue
                    active.remove(item)
                    record.update(state='finished', returncode=returncode, finished_at=now())
                    if returncode != 0 or 'termination_requested' in record:
                        record['python_cleanup_after_abrupt_exit'] = 'unknown'
                    stop = outcome(out, record)
                    if stop:
                        record['stop_reason'] = stop
                        reason = reason or stop
                    persist()
                if interrupted():
                    reason = 'interrupted'
                overdue = any(now_monotonic >= deadline for _, _, deadline in active)
                if active and (interrupted() or overdue or cleanup_deadline is not None):
                    if cleanup_deadline is None:
                        reason = reason or 'child_deadline'
                        for record, _, _ in active:
                            record['termination_requested'] = now()
                        signal_groups(active, signal.SIGTERM)
                        cleanup_deadline = now_monotonic + CLEANUP_SECONDS
                        persist()
                    elif kill_deadline is None and now_monotonic >= cleanup_deadline:
                        signal_groups(active, signal.SIGKILL)
                        kill_deadline = now_monotonic + 1
                    elif kill_deadline is not None and now_monotonic >= kill_deadline:
                        for record, _, _ in active:
                            record.update(state='termination_unconfirmed', python_cleanup_after_abrupt_exit='unknown')
                        persist()
                        active.clear()
                if active:
                    time.sleep(.1)
        finally:
            if active:
                signal_groups(active, signal.SIGTERM)
                deadline = time.monotonic() + CLEANUP_SECONDS
                for _, process, _ in active:
                    try:
                        process.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        break
                live = [item for item in active if item[1].poll() is None]
                signal_groups(live, signal.SIGKILL)
                deadline = time.monotonic() + 1
                for record, process, _ in active:
                    record['python_cleanup_after_abrupt_exit'] = 'unknown'
                    try:
                        process.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        record['state'] = 'termination_unconfirmed'
                        continue
                    record.update(state='finished', returncode=process.returncode, finished_at=now())
                    record['stop_reason'] = outcome(out, record) or 'supervisor_error'
                persist()
        wave_finished(ledger)
        if reason or interrupted():
            return ledger, reason or 'interrupted'
    return ledger, 'completed'


def verify_unchanged(frozen):
    changed = [path for path, checksum in frozen.items() if not Path(path).is_file() or digest(path) != checksum]
    if changed:
        raise ValueError('Frozen inputs changed during run: ' + repr(changed))


def record_progress(out, ledger, private, records, evaluation_errors):
    for record in ledger:
        if record['state'] != 'finished' or record['episode'] in records or record['episode'] in evaluation_errors:
            continue
        try:
            row = evaluate(read_result(out, record), private[record['query_id']])
            save(out / record['episode'] / 'evaluated.json', row)
            records[record['episode']] = row
        except FileNotFoundError:
            continue
        except (ValueError, TypeError, KeyError, OSError) as exc:
            evaluation_errors[record['episode']] = type(exc).__name__
    counts = {
        'planned_tasks': len(ledger),
        'attempted_tasks': sum('attempted_at' in r for r in ledger),
        'started_tasks': sum('pid' in r for r in ledger),
        'terminal_processes': sum(r['state'] == 'finished' for r in ledger),
        'finished_tasks': len(records),
        'unstarted_tasks': sum('pid' not in r for r in ledger),
        'missing_or_ungradable_results': sum(r['state'] == 'finished' and r['episode'] not in records for r in ledger),
        'evaluation_errors': evaluation_errors,
        'unconfirmed_process_terminations': sum(r['state'] == 'termination_unconfirmed' for r in ledger),
        'abrupt_exits_with_unknown_python_cleanup': sum(
            r.get('python_cleanup_after_abrupt_exit') == 'unknown' for r in ledger),

    }
    save(out / 'progress.json', counts)
    save(out / 'summary.json', summarize(list(records.values())))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-name')
    parser.add_argument('--connection', type=Path, default=HERE / 'config/connection.example.json')
    parser.add_argument('--workers', type=int, choices=[3], default=3)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)

    frozen = freeze_files()
    questions, private = load_inputs()
    frozen.update({r['graph_path']: r['graph_raw_sha256'] for r in private.values()})
    jobs = schedule(questions)
    if args.check:
        print(json.dumps({'questions': len(questions), 'scheduled_tasks': len(jobs),
                          'workers': args.workers, 'repeats': 1,
                          'graph_query_limit': CONFIG['max_graph_queries'], 'model_requests': 0,
                          'connection_configured': False}, indent=2))
        return 0

    connection_path = args.connection.resolve()
    connection = launch_gate(connection_path, args.execute)
    frozen.setdefault(str(connection_path), digest(connection_path))
    if not args.run_name:
        parser.error('New --run-name required')
    out = run_path(args.run_name)
    if out.exists() or out.is_symlink():
        raise FileExistsError('Run path already exists; no overwrite/resume/retry')
    python = DockerPython(CONFIG['python_image'], CONFIG['python_timeout_seconds'], CONFIG['python_output_chars'])
    smoke = python.execute('print("python-ready")', {}, [])
    if smoke.get('status') != 'ok' or smoke.get('cleanup') not in {None, 'confirmed'}:
        raise RuntimeError('Python isolation preflight failed; no model requests made')
    verify_unchanged(frozen)
    out.mkdir(exist_ok=False)

    runtime = {**CONFIG, 'model_enabled': True}
    connection_metadata = {k: connection[k] for k in ('enabled', 'base_url', 'model', 'protocol', 'stream', 'api_key_env')}
    save(out / 'manifest.json', {
        'study': runtime, 'connection': connection_metadata, 'sha256': frozen,
        'public_tasks': [{'query_id': q['query_id'], 'arm': arm} for q, arm in jobs],
        'python_preflight': smoke, 'repeats': 1, 'python_executable': sys.executable,
        'workers': args.workers, 'max_concurrent_episodes': args.workers, 'wave_policy': 'same_question',
    })
    save(out / 'launch.json', {'pid': os.getpid(), 'argv': sys.argv, 'started_at': now()})

    signum = None
    def interrupted(received, _frame):
        nonlocal signum
        if signum is None:
            signum = received

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    command = [sys.executable, '-B', str(HERE / 'worker_job.py'), '--execute']
    context = {'connection': str(connection_path), 'connection_sha256': frozen[str(connection_path)],
               'private_sha256': frozen[str(HERE / 'private/evaluation.json')]}
    reason = 'campaign_error'
    records, evaluation_errors = {}, {}
    ledger = [{'query_id': q['query_id'], 'arm': arm, 'state': 'unstarted'} for q, arm in jobs]

    def progress(current):
        record_progress(out, current, private, records, evaluation_errors)
        if evaluation_errors:
            raise ValueError('Saved episode could not be graded')

    try:
        ledger, reason = dispatch(out, jobs, command, lambda: verify_unchanged(frozen),
                                  interrupted=lambda: signum, wave_finished=progress, worker_context=context)
    except Exception as exc:
        save(out / 'campaign-error.json', {'type': type(exc).__name__, 'message': str(exc)})
        if (out / 'ledger.json').exists():
            ledger = json.loads((out / 'ledger.json').read_text())
    finally:
        counts = record_progress(out, ledger, private, records, evaluation_errors)
        try:
            verify_unchanged(frozen)
            unchanged = True
        except (OSError, ValueError):
            unchanged = False
            if reason == 'completed':
                reason = 'frozen_inputs_changed'
        if signum is not None:
            reason = 'interrupted'
        save(out / 'completion.json', {
            **counts, 'finished_at': now(), 'reason': reason, 'signal': signum,
            'model_requests': sum(r['counters']['model_rounds'] for r in records.values()),
            'model_request_count_complete': counts['finished_tasks'] == counts['started_tasks'],
            'input_hashes_unchanged': unchanged,
        })
        for sig, prior in previous.items():
            signal.signal(sig, prior)
    print(json.dumps({'reason': reason, **counts}, ensure_ascii=False, indent=2))
    return 0 if reason == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
