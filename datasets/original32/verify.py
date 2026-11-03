"""Read-only dataset integrity/oracle check; not a model or tool-efficacy test."""
import argparse
from collections import Counter, deque
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def require(condition,message):
    if not condition: raise ValueError(message)

def sha(data): return hashlib.sha256(data).hexdigest()
def rows(name): return [json.loads(x) for x in (ROOT/name).read_text().splitlines() if x.strip()]
def indexed(data):
    result={r['query_id']:r for r in data}
    require(len(result)==len(data),'DuplicateQueryID'); return result

def graph_data(root,filename,expected):
    require(Path(filename).name==filename,'UnsafeFilename')
    paths=list(root.rglob(filename));require(len(paths)==1,'MissingOrAmbiguousGraph:'+filename)
    with paths[0].open('rb') as f: raw=f.read(16*1024*1024+1)
    require(len(raw)<=16*1024*1024,'FileCapExceeded')
    require(sha(raw)==expected,'GraphHashMismatch:'+filename)
    graph=json.loads(raw);ns=[n['id'] for n in graph['nodes']]
    require(len(ns)==len(set(ns)),'DuplicateNodes')
    require(('edges' in graph) != ('links' in graph),'AmbiguousEdgeField')
    edges=graph.get('edges',graph.get('links'));adj={n:set() for n in ns}
    for e in edges:
        a,b=e['source'],e['target'];require(a in adj and b in adj,'UndeclaredEndpoint')
        adj[a].add(b);adj[b].add(a)
    return adj

def shortest(adj,s,t):
    require(s in adj and t in adj,'MissingEndpoint')
    distances={s:0};queue=deque([s])
    while queue:
        n=queue.popleft()
        if n==t:return distances[n]
        for v in adj[n]:
            if v not in distances:distances[v]=distances[n]+1;queue.append(v)
    return None

def valid_path(adj,path,s,t,bound):
    return (isinstance(path,list) and len(path)>=2 and all(isinstance(n,str) for n in path)
        and path[0]==s and path[-1]==t and len(path)-1<=bound and len(path)==len(set(path))
        and all(a in adj and b in adj[a] for a,b in zip(path,path[1:])))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--graphs-dir',type=Path,required=True)
    ap.add_argument('--followup-fixtures',type=Path)
    args=ap.parse_args()
    manifest=json.loads((ROOT/'MANIFEST.json').read_text())
    for rel,expected in manifest['files'].items():
        require(Path(rel).name==rel,'ManifestPathInvalid')
        require(sha((ROOT/rel).read_bytes())==expected,'DataFileHashMismatch:'+rel)
    qs=rows('questions.jsonl');ans=indexed(rows('answers.private.jsonl'))
    assets=indexed(rows('assets.jsonl'));prov=indexed(rows('provenance.private.jsonl'))
    require([q['query_id'] for q in qs]==['expanded-%03d'%i for i in range(1,33)],'QuestionOrderChanged')
    ids={q['query_id'] for q in qs};require(ids==set(ans)==set(assets)==set(prov),'MismatchedRows')
    require(len({q['instance_id'] for q in qs})==32,'DuplicateTask')
    require(len({q['graph_raw_sha256'] for q in qs})==32,'DuplicateGraph')
    pairs={tuple(sorted((q['start'],q['target']))) for q in qs};require(len(pairs)==32,'DuplicateEndpointPair')
    distances=Counter();repos=Counter();graph_replays=0
    for q in qs:
        a=assets[q['query_id']];r=ans[q['query_id']];p=prov[q['query_id']]
        require(q['max_hops']==4,'ChangedPublicHopBound')
        require(a['graph_filename']==q['graph_file']==p['graph_file'],'FilenameMismatch')
        require(a['graph_sha256']==q['graph_raw_sha256']==p['graph_raw_sha256'],'AssetHashMappingMismatch')
        require(a['instance_id']==q['instance_id']==p['instance_id'],'TaskMappingMismatch')
        require(a['repo']==p['repo'],'RepositoryMismatch')
        require(a['graph_key']==a['repo'].split('/')[-1]+'_'+a['base_commit'],'GraphKeyMismatch')
        require(a['graph_filename']==a['graph_key']+'.json','RevisionFilenameMismatch')
        require((q['start'],q['target'])==(p['start'],p['target']),'EndpointMismatch')
        adj=graph_data(args.graphs_dir,q['graph_file'],q['graph_raw_sha256'])
        d=shortest(adj,q['start'],q['target'])
        require(d==r['distance']==r['shortest_distance']==p['distance'] and d in (1,2,3,4),'DistanceMismatch')
        for key in ('path','raw_bfs_path'):
            require(valid_path(adj,r[key],q['start'],q['target'],q['max_hops']),'InvalidReferencePath:'+q['query_id'])
            require(len(r[key])-1==d,'ReferenceNotShortest:'+q['query_id'])
        distances[d]+=1;repos[a['repo']]+=1;graph_replays+=1
    require(distances==Counter({1:8,2:8,3:8,4:8}),'DistanceDistributionChanged')
    require(repos==Counter({'Textualize/rich':15,'fastapi/fastapi':15,'psf/requests':2}),'RepoDistributionChanged')
    report=dict(original32_validated=graph_replays,shortest_distance_counts=dict(distances),repositories=dict(repos),model_requests=0,tool_calls=0,scope='dataset/oracle validation, not historical tool-output replay or model grading')
    if args.followup_fixtures:
        raw=args.followup_fixtures.read_bytes()
        require(sha(raw)==manifest['existing_followup18_sha256'],'ExistingFollowup18Changed')
        other=json.loads(raw);require(len(other)==18,'FollowupCountMismatch')
        require(not ids & {x['query_id'] for x in other},'OverlappingQueryIDs')
        require(not {q['instance_id'] for q in qs} & {x['instance_id'] for x in other},'OverlappingTasks')
        require(not {q['graph_raw_sha256'] for q in qs} & {x['graph_sha256'] for x in other},'OverlappingGraphFiles')
        otherpairs=set()
        for x in other:
            a=x['arguments'];require(len(a['source_nodes'])==len(a['target_nodes'])==1,'NotSinglePair')
            s,t=a['source_nodes'][0],a['target_nodes'][0];otherpairs.add(tuple(sorted((s,t))))
            adj=graph_data(args.graphs_dir,x['graph_filename'],x['graph_sha256'])
            d=shortest(adj,s,t);require(d is not None and 1<=d<=a['max_hops'],'InvalidFollowupPositive')
        require(not pairs & otherpairs,'OverlappingEndpointPairs')
        report.update(existing_followup18_unchanged=True,followup18_graphs_validated=18,distinct_tasks_total=50,distinct_endpoint_pairs_total=50)
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
