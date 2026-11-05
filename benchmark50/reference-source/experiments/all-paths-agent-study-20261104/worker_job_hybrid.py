"""Run one explicitly enabled episode in a dedicated process."""
import argparse
import json
from pathlib import Path
from hashlib import sha256
import signal
import sys

sys.dont_write_bytecode = True

from episode import run_episode
from python_tool import DockerPython
from support import CONFIG, HERE, make_backend, save
from hybrid_transport import HybridTransport, transport_usage, validate_hybrid_connection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('job', type=Path)
    args = parser.parse_args()

    def interrupt(*_):
        raise KeyboardInterrupt('Owned episode interrupted')

    signal.signal(signal.SIGTERM, interrupt)
    job = json.loads(args.job.read_text())
    qid, arm = job['query_id'], job['arm']
    question = job['question']
    if set(question) != {'query_id', 'start', 'target', 'max_hops'} or question['query_id'] != qid:
        raise ValueError('Expected a public-only question')
    if not args.execute:
        parser.error('No model call without explicit --execute')
    connection_bytes = Path(job['connection']).read_bytes()
    if sha256(connection_bytes).hexdigest() != job['connection_sha256']:
        raise ValueError('Connection changed after campaign freeze')
    connection = validate_hybrid_connection(json.loads(connection_bytes))
    private_bytes = (HERE / 'private/evaluation.json').read_bytes()
    if sha256(private_bytes).hexdigest() != job['private_sha256']:
        raise ValueError('Evaluator inputs changed after campaign freeze')
    refs = json.loads(private_bytes)
    reference = next(ref for ref in refs if ref['query_id'] == qid)
    if any(reference[k] != v for k, v in question.items()):
        raise ValueError('Public/evaluator mismatch')
    backend = make_backend(arm, {k: reference[k] for k in ('graph_path', 'graph_raw_sha256')})
    python = DockerPython(CONFIG['python_image'], CONFIG['python_timeout_seconds'], CONFIG['python_output_chars'])
    transport = HybridTransport(connection, arm)
    out = Path(job['output'])
    owns_output = True
    try:
        run_episode(question, arm, backend, python, transport, out,
                    config={**CONFIG, 'model_enabled': True})
    except FileExistsError:
        owns_output = False
        raise
    finally:
        try:
            result_path = out / 'result.json'
            if owns_output and result_path.is_file():
                row = json.loads(result_path.read_text())
                row['transport_usage'] = transport_usage(out)
                save(result_path, row)
        finally:
            transport.close()


if __name__ == '__main__':
    main()
