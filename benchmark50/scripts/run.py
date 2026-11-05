"""Explicit model campaigns and offline controls with isolated, free-slot workers."""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import re
import sys
from benchmark50.support import (CONFIG,HERE,digest,freeze_files,grade_episode,load_inputs,
                                 make_backend,save,controllers,core)
from benchmark50.runner.episode import run_episode
from benchmark50.runner.python_tool import DockerPython
from benchmark50.runner.transport import ChatTransport,validate_connection
from benchmark50.runner.async_transport import AsyncTransport,validate_async_connection

LABELS={'legacy-tools-agent':'Competition tools',
        'guided-legacy-tools-agent':'Competition tools + search guidance',
        'all-paths-tool-agent':'graph_find_paths'}
REFERENCE_LABELS={'legacy-program':'Competition tools + deterministic program',
                  'all-paths-direct':'graph_find_paths direct'}

def run_path(name, runs_dir='runs'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',name):raise ValueError('Use a simple new run name')
    return Path(runs_dir).resolve()/name

def launch_gate(connection_path,execute):
    if not execute:raise ValueError('No model call without explicit --execute')
    config=json.loads(Path(connection_path).read_text())
    validate=validate_async_connection if config.get('protocol')=='responses_background_stateless' else validate_connection
    return validate(config)

def schedule(questions):
    arms=list(CONFIG['arms']);result=[]
    for i,q in enumerate(questions):
        ordered=arms[i%3:]+arms[:i%3]
        result.extend((q,arm) for arm in ordered)
    return result

def evaluate(row,ref):
    diagnostic_answer=row.get('submitted_answer',row['answer'])
    row['grade']=grade_episode(row['public_question'],diagnostic_answer,ref['expected_paths'],row['observations'])
    grade=row['grade']
    grade['missing_path_count']=grade['expected_path_count']-grade['matched_path_count']
    grade['submission_gradable']=row.get('submitted_answer_parsed',diagnostic_answer is not None)
    row['success']=row['answer'] is not None and row['grade']['exact_set_success']
    row['source']={'graph_sha256':ref['graph_raw_sha256'],'instance_id':ref['instance_id'],'cohort':ref['cohort']}
    return row

def collect_rows(out,private):
    rows=[];seen=set()
    for p in sorted(Path(out).glob('*/result.json')):
        row=json.loads(p.read_text());identity=(row['query_id'],row['arm'])
        if p.parent.name != row['query_id']+'--'+row['arm']:raise ValueError('Task directory identity mismatch')
        if identity in seen or row['arm'] not in CONFIG['arms']:raise ValueError('Duplicate/unknown task identity')
        seen.add(identity);ref=private[row['query_id']]
        if row['public_question'] != {k:ref[k] for k in ('query_id','start','target','max_hops')}:
            raise ValueError('Saved task identity differs from frozen question')
        evaluate(row,ref);save(p.parent/'evaluated.json',row);rows.append(row)
    return rows

def summarize(rows):
    from benchmark50.scripts.aggregate import summarize as summary
    return summary(rows)

def effective_config(image=None):
    cfg=deepcopy(CONFIG)
    for arm,label in LABELS.items():cfg['arms'][arm]['label']=label
    cfg['python_image']=image
    return cfg

def _failure(question,arm,error):
    return dict(query_id=question['query_id'],arm=arm,label=LABELS[arm],public_question=question,
                answer=None,submitted_answer=None,submitted_answer_parsed=False,
                stop_reason='worker_setup_error',error={'type':type(error).__name__,'message':str(error)},
                counters=dict(graph_attempts=0,python_attempts=0,model_rounds=0),
                usage=dict(prompt_tokens=0,completion_tokens=0,unknown_usage_responses=0),observations=[],traces=[])

def worker(job):
    question,reference,arm,out,connection,cfg,host,implementation=job
    path=Path(out);transport=None
    try:
        freeze_files()
        backend=make_backend(arm,reference,implementation)
        python=DockerPython(cfg['python_image'],cfg['python_timeout_seconds'],cfg['python_output_chars'],docker_host=host)
        transport_class=AsyncTransport if connection.get('protocol')=='responses_background_stateless' else ChatTransport
        transport=transport_class(connection)
        row=run_episode(question,arm,backend,python,transport,path,cfg)
        evaluate(row,reference);save(path/'evaluated.json',row)
        return row
    except Exception as exc:
        if (path/'result.json').exists():
            row=json.loads((path/'result.json').read_text());return evaluate(row,reference)
        if path.exists():raise  # Retain incomplete claimed directories; never rerun them.
        path.mkdir(exist_ok=False);row=_failure(question,arm,exc)
        save(path/'result.json',row);evaluate(row,reference);save(path/'evaluated.json',row);return row
    finally:
        if transport:transport.close()

def input_identity(implementation):
    frozen=freeze_files()
    hashes={str(Path(p).relative_to(HERE)):h for p,h in frozen.items()}
    if implementation=='product':
        from code_graph_find_paths.api import bundle_root
        root=bundle_root()
        from code_graph_find_paths import _sources
        hashes['product/_sources.py']=digest(_sources.__file__)
        for name,h in _sources.SOURCE_HASHES.items():
            if digest(root/name)!=h:raise ValueError('Product source mismatch')
            hashes['product/'+name]=h
    return hashes

def deterministic(out,questions,private,implementation):
    out.mkdir(parents=True,exist_ok=False);frozen=input_identity(implementation);rows=[]
    save(out/'manifest.json',dict(mode='deterministic',implementation=implementation,input_hashes=frozen,
                                 model_requests=0,planned_tasks=100,config=effective_config()))
    for i,q in enumerate(questions):
        for name in list(REFERENCE_LABELS)[::1 if i%2==0 else -1]:
            ref=private[q['query_id']]
            arm='legacy-tools-agent' if name=='legacy-program' else 'all-paths-tool-agent'
            meter=core.MeteredInvoker(make_backend(arm,ref,implementation),CONFIG)
            try:
                result=(controllers.run_baseline(q,meter,max_queries=32,max_neighbors=20000)
                        if name=='legacy-program' else controllers.run_candidate(q,meter))
                obs=[dict(name=t['name'],arguments=t['args'],result=t['delivered'])
                     for t in meter.traces if t['name']!='submit_graph_answer' and t.get('delivered') is not None]
                grade=grade_episode(q,meter.submitted,ref['expected_paths'],obs)
                row=dict(query_id=q['query_id'],configuration=name,label=REFERENCE_LABELS[name],
                         answer=meter.submitted,grade=grade,query_attempts=meter.graph_attempts,
                         native_executions=meter.native_calls,stop_reason=result['stop_reason'],traces=meter.traces)
            except Exception as exc:
                row=dict(query_id=q['query_id'],configuration=name,label=REFERENCE_LABELS[name],
                         query_attempts=meter.graph_attempts,execution_error={'type':type(exc).__name__,'message':str(exc)},traces=meter.traces)
            save(out/f"{q['query_id']}--{name}.json",row);rows.append(row)
    summary={}
    for name in REFERENCE_LABELS:
        group=[r for r in rows if r['configuration']==name]
        summary[name]=dict(label=REFERENCE_LABELS[name],tasks=len(group),
            exact_sets=sum(r.get('grade',{}).get('exact_set_success',False) for r in group),
            certified_complete=sum(r.get('grade',{}).get('certified_success',False) for r in group),
            mean_graph_queries=sum(r['query_attempts'] for r in group)/len(group),
            paths_matched=sum(r.get('grade',{}).get('matched_path_count',0) for r in group),
            errors=sum('execution_error' in r for r in group))
    unchanged=input_identity(implementation)==frozen
    save(out/'summary.json',summary);save(out/'completion.json',dict(finished_tasks=len(rows),planned_tasks=100,
          status='complete' if len(rows)==100 else 'partial',model_requests=0,input_hashes_unchanged=unchanged))
    if not unchanged:raise ValueError('Frozen inputs changed')
    return summary

def model_campaign(args,questions,private,connection):
    if not args.python_image:raise ValueError('--python-image must be the recipient-built local image ID')
    cfg=effective_config(args.python_image)
    out=run_path(args.run_name,args.runs_dir)
    identity=dict(config=cfg,connection=connection,implementation=args.implementation,input_hashes=input_identity(args.implementation))
    jobs=schedule(questions)
    if args.resume:
        previous=json.loads((out/'manifest.json').read_text())
        if previous['identity']!=identity:raise ValueError('Resume requires unchanged inputs, model, sampling and implementation')
        collect_rows(out,private)  # Validate retained outcomes before any new execution.
        expected={q['query_id']+'--'+a for q,a in jobs}
        for claim in (out/'claims').glob('*.json'):
            value=json.loads(claim.read_text())
            if claim.stem not in expected or claim.stem != value['query_id']+'--'+value['arm']:
                raise ValueError('Claimed task identity mismatch')
    elif out.exists():
        raise FileExistsError('Run already exists; choose a new name or --resume')
    python=DockerPython(args.python_image,docker_host=args.docker_host)
    smoke=python.execute('import sys; print(sys.version); print("python-ready")',{},[])
    if smoke.get('status')!='ok' or smoke.get('cleanup')!='confirmed':
        raise RuntimeError('Python isolation preflight failed; no model requests made: '+json.dumps(smoke))
    if not args.resume:
        out.mkdir(parents=True,exist_ok=False)
        save(out/'manifest.json',dict(mode='model',identity=identity,repeats=1,planned_tasks=150,
            tasks=[dict(query_id=q['query_id'],arm=a) for q,a in jobs],python_preflight=smoke,
            protocol=connection.get('protocol','chat_completions'),workers=args.workers,python_version=sys.version))
    claims=out/'claims';claims.mkdir(exist_ok=True)
    pending=[]
    for q,arm in jobs:
        task_dir=out/f"{q['query_id']}--{arm}"
        if not task_dir.exists() and not (claims/(task_dir.name+'.json')).exists():pending.append((q,private[q['query_id']],arm,str(task_dir),connection,cfg,args.docker_host,args.implementation))
    attempts=out/'launches';attempts.mkdir(exist_ok=True)
    save(attempts/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json'),dict(workers=args.workers,pending=len(pending),resume=args.resume))
    try:
        with ProcessPoolExecutor(max_workers=args.workers,mp_context=multiprocessing.get_context('spawn'),max_tasks_per_child=1) as pool:
            waiting=iter(pending);futures={}
            def fill_slots():
                while len(futures)<args.workers:
                    job=next(waiting,None)
                    if job is None:return
                    claim=claims/(job[0]['query_id']+'--'+job[2]+'.json')
                    with claim.open('x') as f:json.dump(dict(query_id=job[0]['query_id'],arm=job[2]),f)
                    futures[pool.submit(worker,job)]=job
            try:
                fill_slots()
                while futures:
                    done,_=wait(futures,return_when=FIRST_COMPLETED)
                    stop=False
                    for future in done:
                        job=futures.pop(future)
                        try:row=future.result()
                        except Exception as exc:
                            save(out/(job[0]['query_id']+'--'+job[2]+'.worker-error.json'),
                                 {'type':type(exc).__name__,'message':str(exc),'request_count':'unknown'})
                            stop=True
                            continue
                        if row['stop_reason']=='python_cleanup_unconfirmed':stop=True
                    rows=collect_rows(out,private);save(out/'summary.json',summarize(rows))
                    save(out/'progress.json',dict(finished=len(rows),planned=150))
                    if stop:raise RuntimeError('Worker state or container cleanup unconfirmed; no further tasks launched')
                    fill_slots()
            finally:
                for future in futures:future.cancel()
    finally:
        rows=collect_rows(out,private);save(out/'summary.json',summarize(rows))
        claimed={p.stem for p in claims.glob('*.json')}
        claimed.update(q['query_id']+'--'+a for q,a in jobs if (out/(q['query_id']+'--'+a)).is_dir())
        unknown=sorted(name for name in claimed if not (out/name/'result.json').is_file())
        known=sum(r['counters']['model_rounds'] for r in rows)
        save(out/'completion.json',dict(status='complete' if len(rows)==150 else 'partial',finished_tasks=len(rows),planned_tasks=150,
            model_requests=known,known_model_requests=known,request_count_complete=not unknown,unknown_execution_tasks=unknown,
            input_hashes_unchanged=input_identity(args.implementation)==identity['input_hashes']))
    return summarize(rows)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--graph-dir',required=True);p.add_argument('--run-name');p.add_argument('--runs-dir',default='runs')
    p.add_argument('--mode',choices=['model','deterministic'],default='model')
    p.add_argument('--implementation',choices=['historical','product'],default='historical')
    p.add_argument('--check',action='store_true');p.add_argument('--execute',action='store_true');p.add_argument('--resume',action='store_true')
    p.add_argument('--connection',type=Path,default=HERE/'config/connection.example.json')
    p.add_argument('--workers',type=int,default=3);p.add_argument('--python-image');p.add_argument('--docker-host')
    args=p.parse_args(argv)
    if args.workers<1:raise ValueError('Workers must be positive')
    frozen=freeze_files();questions,private=load_inputs(args.graph_dir)
    if args.check:
        result=dict(questions=len(questions),scheduled_tasks=len(schedule(questions)),graph_query_limit=CONFIG['max_graph_queries'],
                    source_files=len(frozen),model_requests=0,implementation=args.implementation)
        if args.python_image:
            result['python_smoke']=DockerPython(args.python_image,docker_host=args.docker_host).execute('print("python-ready")',{},[])
            if result['python_smoke'].get('status')!='ok' or result['python_smoke'].get('cleanup')!='confirmed':raise RuntimeError('Isolation smoke failed')
        print(json.dumps(result,indent=2));return
    if not args.run_name:raise ValueError('New --run-name required')
    if args.mode=='deterministic':
        if args.execute or args.resume:raise ValueError('Deterministic runs require a new name and no --execute')
        result=deterministic(run_path(args.run_name,args.runs_dir),questions,private,args.implementation)
    else:
        connection=launch_gate(args.connection,args.execute)
        result=model_campaign(args,questions,private,connection)
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
