"""Read-only bindings, public input preparation, and evaluator-only scoring."""
from collections import deque
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import jsonschema

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OLD = HERE / 'backends'
CONFIG = json.loads((HERE/'config/study.json').read_text())
SCHEMAS = json.loads((HERE/'config/tools.json').read_text())


def read_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from benchmark50.backends import runtime as core, controllers
from benchmark50.grading import protocol as scorer
from benchmark50.integrity import verify_sources


def digest(path): return sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    tmp.replace(path)


def load_inputs(graph_dir):
    from benchmark50.dataset import load_inputs as load
    return load(graph_dir)


def make_backend(arm, reference, implementation="historical"):
    args=(Path(reference['graph_path']),reference['graph_raw_sha256'])
    if arm=='all-paths-tool-agent':
        if implementation == 'product':
            from benchmark50.backends.product import ProductCandidate
            return ProductCandidate(*args,CONFIG)
        if implementation != 'historical': raise ValueError('Unknown implementation')
        return core.NativeCandidate(*args,CONFIG)
    return core.NativeBaseline(*args)


def tool_definitions(arm):
    names=CONFIG['arms'][arm]['graph_tools']+['execute_python','submit_graph_answer']
    return [{'type':'function','function':SCHEMAS[n]} for n in names]


def argument_error(name,args):
    if name not in SCHEMAS: return 'Unknown tool'
    try: jsonschema.Draft202012Validator(SCHEMAS[name]['parameters']).validate(args)
    except jsonschema.ValidationError as exc: return exc.message
    return None


def canonical(node): return node.rsplit(';',1)[-1]


def evidence_edges(observations):
    edges=set()
    def add(a,b):
        a,b=canonical(a),canonical(b)
        if a!=b: edges.add(tuple(sorted((a,b))))
    for obs in observations:
        name,data=obs['name'],obs['result']
        if not isinstance(data,dict) or data.get('status')=='error': continue
        if name=='get_code_neighbors':
            if data.get('is_truncated') and len(data.get('neighbors',[]))==1: continue
            for node in data.get('neighbors',[]): add(data['node'],node)
        elif name=='get_code_subgraph':
            for edge in data.get('edges',[]): add(edge['from'],edge['to'])
        elif name=='graph_find_paths':
            for edge in data.get('edges',[]):
                a,b=edge['nodes'];add(data['nodes'][a],data['nodes'][b])
    return edges


def complete_proof(question,observations):
    """Recognize sufficient certificates; failure to recognize is not missing paths."""
    start,target,hops=question['start'],question['target'],question['max_hops']
    if start==target: return True
    neighbors={}
    for obs in observations:
        name,data,args=obs['name'],obs['result'],obs['arguments']
        if not isinstance(data,dict) or data.get('status')=='error': continue
        if name=='graph_find_paths' and data.get('complete') is True:
            if args.get('max_hops',4)!=hops: continue
            for pair in data.get('pairs',[]):
                s=args['source_nodes'][pair['source_index']];t=args['target_nodes'][pair['target_index']]
                if canonical(s)==start and canonical(t)==target and pair.get('complete') is True:
                    return True
        if name=='get_code_neighbors' and data.get('status')=='ok':
            vals=data.get('neighbors',[])
            if (not data.get('is_truncated') and not data.get('truncated')
                and not args.get('edge_type') and len(vals)<args.get('max_neighbors',20000)):
                neighbors[canonical(data['node'])]={canonical(n) for n in vals}
    def covered(root,radius):
        seen={root};queue=deque([(root,0)])
        while queue:
            node,depth=queue.popleft()
            if depth>=radius: continue
            if node not in neighbors: return False
            for child in neighbors[node]-seen:
                seen.add(child);queue.append((child,depth+1))
        return True
    return any(covered(start,r) and covered(target,hops-r) for r in range(hops+1))


def grade_episode(question,answer,expected_paths,observations):
    proof=complete_proof(question,observations)
    g=scorer.grade(question,answer,expected_paths,evidence_edges(observations),proof)
    # Primary outcome follows the approved exact-set criterion. A certificate is
    # secondary; absence of our recognized proof does not imply a wrong set.
    claim=isinstance(answer,dict) and answer.get('complete') is True
    g['false_complete_claim']=claim and not g['exact_set_success']
    g['unverified_complete_claim']=claim and g['exact_set_success'] and not proof
    return g


def freeze_files():
    verify_sources()
    return {str(HERE / p): h for p, h in json.loads((HERE/'MANIFEST.json').read_text())['files'].items()}
