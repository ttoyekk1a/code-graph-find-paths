"""Explicitly enabled serial150-task campaign. Import and --check never call a model."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import signal
from statistics import mean
import sys

from episode import run_episode
from python_tool import DockerPython
from support import CONFIG,HERE,digest,freeze_files,grade_episode,load_inputs,make_backend,save
from transport import ChatTransport,validate_connection


def run_path(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',name):raise ValueError('Use a simple new run name')
    return HERE/'runs'/name


def launch_gate(connection_path,execute):
    if not execute:raise ValueError('No model call without explicit --execute')
    return validate_connection(json.loads(Path(connection_path).read_text()))


def schedule(questions):
    arms=list(CONFIG['arms']);result=[]
    for i,q in enumerate(questions):
        ordered=arms[i%3:]+arms[:i%3]
        result.extend((q,arm) for arm in ordered)
    return result


def summarize(rows):
    result={}
    for arm,settings in CONFIG['arms'].items():
        group=[r for r in rows if r['arm']==arm]
        result[settings['label']]={'started':len(group),'planned':50,
            'exact_success':sum(r.get('success',False) for r in group),
            'mean_path_recall':mean(r['grade']['recall'] for r in group) if group else None,
            'mean_graph_attempts':mean(r['counters']['graph_attempts'] for r in group) if group else None,
            'mean_python_attempts':mean(r['counters']['python_attempts'] for r in group) if group else None,
            'mean_model_rounds':mean(r['counters']['model_rounds'] for r in group) if group else None,
            'false_complete_claims':sum(r['grade']['false_complete_claim'] for r in group),
            'prompt_tokens_known':sum(r['usage']['prompt_tokens'] for r in group),
            'completion_tokens_known':sum(r['usage']['completion_tokens'] for r in group),
            'unknown_usage_responses':sum(r['usage']['unknown_usage_responses'] for r in group)}
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
    """Recover terminal or interrupted episode records without rerunning anything."""
    rows=[]
    for p in sorted(out.glob('*/result.json')):
        row=json.loads(p.read_text());ref=private[row['query_id']]
        evaluate(row,ref)
        save(p.parent/'evaluated.json',row);rows.append(row)
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-name');parser.add_argument('--connection',type=Path,default=HERE/'config/connection.example.json')
    parser.add_argument('--execute',action='store_true');parser.add_argument('--check',action='store_true')
    args=parser.parse_args(argv)
    frozen=freeze_files();questions,private=load_inputs()
    if args.check:
        print(json.dumps({'questions':len(questions),'scheduled_tasks':len(schedule(questions)),
            'graph_query_limit':CONFIG['max_graph_queries'],'model_requests':0,'connection_configured':False},indent=2));return
    connection=launch_gate(args.connection,args.execute)
    if not args.run_name:raise ValueError('New --run-name required')
    out=run_path(args.run_name)
    if out.exists():raise FileExistsError('Run path already exists; no overwrite/resume/retry')
    python=DockerPython(CONFIG['python_image'],CONFIG['python_timeout_seconds'],CONFIG['python_output_chars'])
    smoke=python.execute('print("python-ready")',{},[])
    if smoke.get('status')!='ok' or smoke.get('cleanup') not in {None,'confirmed'}:
        raise RuntimeError('Python isolation preflight failed; no model requests made')
    out.mkdir(exist_ok=False)
    jobs=schedule(questions);records=[];reason='completed';transport=None
    manifest={'study':CONFIG,'connection':{k:v for k,v in connection.items() if k not in {'api_key_default'}},
        'sha256':frozen,'public_tasks':[{'query_id':q['query_id'],'arm':a} for q,a in jobs],
        'python_preflight':smoke,'repeats':1,'python_executable':sys.executable}
    save(out/'manifest.json',manifest)
    save(out/'launch.json',{'pid':os.getpid(),'argv':sys.argv,'started_at':datetime.now(timezone.utc).isoformat()})
    def interrupted(*_):raise KeyboardInterrupt('Owned campaign interrupted')
    prior=signal.signal(signal.SIGTERM,interrupted)
    try:
        transport=ChatTransport(connection)
        for q,arm in jobs:
            changed=[p for p,h in frozen.items() if digest(p)!=h]
            if changed:raise ValueError('Frozen inputs changed during run: '+repr(changed))
            ref=private[q['query_id']];backend=make_backend(arm,ref)
            row=run_episode(q,arm,backend,python,transport,out/f"{q['query_id']}--{arm}")
            evaluate(row,ref)
            save(out/f"{q['query_id']}--{arm}"/'evaluated.json',row);records.append(row)
            save(out/'summary.json',summarize(records))
            save(out/'progress.json',{'finished':len(records),'planned':150})
            unknown=[t for t in row['traces'] if isinstance(t.get('raw_result'),dict) and t['raw_result'].get('cleanup')=='unknown']
            if unknown:reason='python_cleanup_unconfirmed';break
            if row['stop_reason'] in {'model_error','runner_error','interrupted','python_cleanup_unconfirmed'}:reason=row['stop_reason'];break
    except BaseException as exc:
        reason='interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'campaign_error'
        save(out/'campaign-error.json',{'type':type(exc).__name__,'message':str(exc)})
        raise
    finally:
        signal.signal(signal.SIGTERM,prior)
        if transport:transport.close()
        records=collect_rows(out,private)
        save(out/'summary.json',summarize(records))
        save(out/'completion.json',{'finished_at':datetime.now(timezone.utc).isoformat(),
             'finished_tasks':len(records),'planned_tasks':150,'reason':reason,
             'model_requests':sum(r['counters']['model_rounds'] for r in records),
             'input_hashes_unchanged':all(digest(p)==h for p,h in frozen.items())})
    print(json.dumps(summarize(records),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
