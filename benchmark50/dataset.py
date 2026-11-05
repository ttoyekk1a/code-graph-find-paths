"""Host-only dataset loading, raw asset verification and independent enumeration."""
from collections import Counter
import importlib.util
import json
from pathlib import Path
from benchmark50.integrity import ROOT, digest, verify_sources

PUBLIC_KEYS = {'query_id', 'start', 'target', 'max_hops'}

def load_inputs(graph_dir):
    verify_sources()
    directory = Path(graph_dir).expanduser().resolve()
    questions = [json.loads(line) for line in (ROOT/'data/questions.jsonl').read_text().splitlines()]
    refs = json.loads((ROOT/'evaluator/reference-answers.json').read_text())
    assets = json.loads((ROOT/'data/graph-assets.json').read_text())
    private = {r['query_id']: dict(r) for r in refs}
    by_id = {a['query_id']: a for a in assets}
    ids = {q['query_id'] for q in questions}
    if len(questions) != 50 or len(refs) != 50 or len(assets) != 50 or len(ids) != 50 or ids != set(private) or ids != set(by_id):
        raise ValueError('Expected the fixed 50 questions, answers and graph assets')
    if len({a['graph_filename'] for a in assets}) != 50 or sum(len(r['expected_paths']) for r in refs) != 142:
        raise ValueError('Expected 50 distinct graphs and 142 paths')
    for q in questions:
        if set(q) != PUBLIC_KEYS or q['max_hops'] != 4: raise ValueError('Invalid public question')
        ref, asset = private[q['query_id']], by_id[q['query_id']]
        if any(q[k] != ref[k] for k in q): raise ValueError('Question/reference mismatch')
        name = ref['graph_file']
        if Path(name).name != name or name != asset['graph_filename']: raise ValueError('Invalid graph filename')
        if ref['graph_raw_sha256'] != asset['sha256']: raise ValueError('Asset/reference hash mismatch')
        if any(ref[k] != asset[k] for k in ('cohort', 'instance_id')): raise ValueError('Source mismatch')
        path = directory/name
        if path.stat().st_size != asset['bytes'] or digest(path) != asset['sha256']:
            raise ValueError('Graph bytes/hash mismatch: '+name)
        ref['graph_path'] = str(path)
    return questions, private

def verify_dataset(graph_dir):
    questions, refs = load_inputs(graph_dir)
    path = ROOT/'reference-source/evidence/all-paths-v1-20261104/oracle.py'
    spec = importlib.util.spec_from_file_location('_benchmark50_independent_oracle', path)
    oracle = importlib.util.module_from_spec(spec); spec.loader.exec_module(oracle)
    matched = 0; repositories = Counter()
    for q in questions:
        ref = refs[q['query_id']]
        graph = json.loads(Path(ref['graph_path']).read_bytes())
        nodes = [n['id'] for n in graph['nodes']]
        if len(nodes) != len(set(nodes)): raise ValueError('Duplicate nodes')
        adjacency = oracle.graph_adjacency(nodes, graph['edges'])
        paths, _ = oracle.all_paths(adjacency, q['start'], q['target'], q['max_hops'])
        expected = [tuple(p) for p in ref['expected_paths']]
        if len(set(expected)) != len(expected) or set(paths) != set(expected):
            raise ValueError('Independent oracle mismatch: '+q['query_id'])
        matched += len(paths); repositories[ref['graph_file'].split('_',1)[0]] += 1
    if dict(repositories) != {'rich':25,'fastapi':23,'requests':2}: raise ValueError('Repository counts changed')
    return dict(questions=50, graphs=50, paths=matched, repositories=dict(repositories),
                graph_bytes=sum(a['bytes'] for a in json.loads((ROOT/'data/graph-assets.json').read_text())),
                independent_oracle_verified=True, model_requests=0)
