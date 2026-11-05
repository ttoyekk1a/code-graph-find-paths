"""Explicitly enabled Gemma-4-31B hybrid campaign, one question per wave."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys

sys.dont_write_bytecode = True

from async_transport import CODEC_PATH, CODEC_SHA256
from hybrid_transport import CHAT_TIMEOUT_SECONDS, summarize_transport, validate_hybrid_connection
from python_tool import DockerPython
from run_agents import run_path, schedule, summarize
from run_parallel import dispatch, now, record_progress, verify_unchanged
from support import CONFIG, HERE, freeze_files, load_inputs, save


CONNECTION = HERE / 'config/connection.hybrid-gemma31b.json'
RUN_NAME = 'gemma31b-hybrid-once-01'


def hybrid_progress(out, ledger, private, records, evaluation_errors):
    counts = record_progress(out, ledger, private, records, evaluation_errors)
    rows = list(records.values())
    summary = summarize(rows)
    for arm, settings in CONFIG['arms'].items():
        group = [row for row in rows if row['arm'] == arm]
        started = sum(record['arm'] == arm and 'pid' in record for record in ledger)
        summary[settings['label']]['physical_transport_usage'] = summarize_transport(
            group, missing=started - len(group))
    usage = summarize_transport(rows, missing=counts['started_tasks'] - len(rows))
    save(out / 'summary.json', {**summary, 'physical_transport_usage': usage})
    return counts, usage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-name')
    parser.add_argument('--connection', type=Path)
    parser.add_argument('--workers', type=int, choices=[3], default=3)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)

    if not args.check:
        if not args.execute:
            parser.error('No model call without explicit --execute')
        if args.connection is None or args.connection.resolve() != CONNECTION:
            parser.error('Explicit frozen hybrid connection file required')
        if args.run_name != RUN_NAME:
            parser.error('Explicit fresh hybrid campaign name required')

    frozen = freeze_files()
    frozen[str(CODEC_PATH)] = CODEC_SHA256
    verify_unchanged({str(CODEC_PATH): CODEC_SHA256})
    questions, private = load_inputs()
    frozen.update({r['graph_path']: r['graph_raw_sha256'] for r in private.values()})
    jobs = schedule(questions)
    if args.check:
        validate_hybrid_connection(json.loads(CONNECTION.read_text()))
        print(json.dumps({'questions': len(questions), 'scheduled_tasks': len(jobs),
                          'workers': args.workers, 'repeats': 1,
                          'graph_query_limit': CONFIG['max_graph_queries'], 'model_requests': 0,
                          'connection_configured': True}, indent=2))
        return 0

    connection_path = args.connection.resolve()
    connection = validate_hybrid_connection(json.loads(connection_path.read_text()))
    verify_unchanged(frozen)
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
    connection_metadata = {k: connection[k] for k in ('enabled', 'base_url', 'model', 'protocol', 'stream', 'api_key_env',
                                                        'async_default_arms', 'fallback_status_codes',
                                                        'sticky_async_after_timeout')}
    save(out / 'manifest.json', {
        'study': runtime, 'connection': connection_metadata, 'sha256': frozen,
        'public_tasks': [{'query_id': q['query_id'], 'arm': arm} for q, arm in jobs],
        'chat_timeout_seconds': CHAT_TIMEOUT_SECONDS, 'python_preflight': smoke, 'repeats': 1, 'python_executable': sys.executable,
        'workers': args.workers, 'max_concurrent_episodes': args.workers, 'wave_policy': 'same_question',
    })
    save(out / 'launch.json', {'pid': os.getpid(), 'argv': sys.argv, 'started_at': now()})

    signum = None
    def interrupted(received, _frame):
        nonlocal signum
        if signum is None:
            signum = received

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    command = [sys.executable, '-B', str(HERE / 'worker_job_hybrid.py'), '--execute']
    context = {'connection': str(connection_path), 'connection_sha256': frozen[str(connection_path)],
               'private_sha256': frozen[str(HERE / 'private/evaluation.json')]}
    reason = 'campaign_error'
    records, evaluation_errors = {}, {}
    ledger = [{'query_id': q['query_id'], 'arm': arm, 'state': 'unstarted'} for q, arm in jobs]

    def progress(current):
        hybrid_progress(out, current, private, records, evaluation_errors)
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
        counts, usage = hybrid_progress(out, ledger, private, records, evaluation_errors)
        try:
            verify_unchanged(frozen)
            unchanged = True
        except (OSError, ValueError):
            unchanged = False
            if reason == 'completed':
                reason = 'frozen_inputs_changed'
        if signum is not None:
            reason = 'interrupted'
        logical_rounds = sum(r['counters']['model_rounds'] for r in records.values())
        save(out / 'completion.json', {
            **counts, 'finished_at': now(), 'reason': reason, 'signal': signum,
            'logical_model_rounds': logical_rounds, 'model_requests': logical_rounds,
            'model_request_count_complete': counts['finished_tasks'] == counts['started_tasks'],
            'input_hashes_unchanged': unchanged, 'physical_transport_usage': usage,
        })
        for sig, prior in previous.items():
            signal.signal(sig, prior)
    print(json.dumps({'reason': reason, **counts}, ensure_ascii=False, indent=2))
    return 0 if reason == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
