"""Fresh32-query deterministic references. No model imports, endpoint or requests."""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
from statistics import mean

from support import CONFIG,HERE,core,controllers,digest,freeze_files,grade_episode,load_inputs,make_backend,save

LABELS={'legacy-program':'旧工具＋确定性搜索程序','all-paths-direct':'全路径工具直接执行'}


def reference_config():
    cfg=deepcopy(CONFIG);cfg['max_graph_queries']=32;return cfg


def run(name='reference32-01'):
    from run_agents import run_path
    out=run_path(name);out.mkdir(exist_ok=False)
    cfg=reference_config();frozen=freeze_files();questions,private=load_inputs();rows=[]
    save(out/'manifest.json',{'config':cfg,'sha256':frozen,'labels':LABELS,'model_requests':0,
         'scope':'deterministic native-core reference; not an Agent or optimality proof'})
    save(out/'launch.json',{'started_at':datetime.now(timezone.utc).isoformat(),'model_requests':0})
    for i,q in enumerate(questions):
        for name in list(LABELS)[::1 if i%2==0 else -1]:
            ref=private[q['query_id']]
            arm='legacy-tools-agent' if name=='legacy-program' else 'all-paths-tool-agent'
            meter=core.MeteredInvoker(make_backend(arm,ref),cfg)
            try:
                result=(controllers.run_baseline(q,meter,max_queries=32,max_neighbors=20000)
                        if name=='legacy-program' else controllers.run_candidate(q,meter))
                obs=[{'name':t['name'],'arguments':t['args'],'result':t.get('delivered')}
                     for t in meter.traces if t['name']!='submit_graph_answer' and t.get('delivered') is not None]
                grade=grade_episode(q,meter.submitted,ref['expected_paths'],obs)
                row={'query_id':q['query_id'],'configuration':name,'label':LABELS[name],
                     'answer':meter.submitted,'grade':grade,'query_attempts':meter.graph_attempts,
                     'native_executions':meter.native_calls,'stop_reason':result['stop_reason'],
                     'traces':meter.traces,'source':{'instance_id':ref['instance_id'],'graph_sha256':ref['graph_raw_sha256']}}
            except Exception as exc:
                row={'query_id':q['query_id'],'configuration':name,'label':LABELS[name],
                     'execution_error':{'type':type(exc).__name__,'message':str(exc)},'traces':meter.traces}
            save(out/f"{q['query_id']}--{name}.json",row);rows.append(row)
    summaries={}
    for name,label in LABELS.items():
        group=[r for r in rows if r['configuration']==name];valid=[r for r in group if 'grade' in r]
        summaries[label]={'tasks':len(group),'exact_sets':sum(r['grade']['exact_set_success'] for r in valid),
            'certified_complete':sum(r['grade']['certified_success'] for r in valid),
            'mean_graph_queries':mean(r['query_attempts'] for r in valid) if valid else None,
            'max_graph_queries':max((r['query_attempts'] for r in valid),default=None),
            'paths_matched':sum(r['grade']['matched_path_count'] for r in valid),
            'errors':len(group)-len(valid)}
    unchanged=all(digest(p)==h for p,h in frozen.items())
    save(out/'results.json',{'summary':summaries,'episodes':rows,'model_requests':0})
    save(out/'completion.json',{'episodes':len(rows),'model_requests':0,'input_hashes_unchanged':unchanged})
    if not unchanged:raise ValueError('Frozen inputs changed')
    return summaries


if __name__=='__main__':
    import json
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-name',default='reference32-01')
    print(json.dumps(run(parser.parse_args().run_name),ensure_ascii=False,indent=2))
