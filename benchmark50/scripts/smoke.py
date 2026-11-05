"""Run three scripted Chat tool loops in fresh processes; no model or network requests."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import httpx
from benchmark50.support import HERE,load_inputs,make_backend,tool_definitions,save
from benchmark50.runner.episode import run_episode
from benchmark50.runner.python_tool import DockerPython
from benchmark50.runner.transport import ChatTransport
from benchmark50.scripts.run import effective_config,evaluate,LABELS,run_path


def smoke_worker(job):
    question,reference,arm,root,image,host=job
    count=0
    def handler(request):
        nonlocal count
        body=json.loads(request.content)
        assert body['stream'] is False and body['temperature']==0.2 and body['max_tokens']==4096
        expected=(HERE/'config/system-prompt.txt').read_text()
        if arm=='guided-legacy-tools-agent':expected+='\n\n'+(HERE/'config/search-guidance.md').read_text()
        assert body['messages'][0]=={'role':'system','content':expected}
        assert body['messages'][1]=={'role':'user','content':json.dumps(question)}
        assert set(question)=={'query_id','start','target','max_hops'}
        assert body['tools']==tool_definitions(arm)
        if count==0:
            if arm=='all-paths-tool-agent':name,args='graph_find_paths',dict(source_nodes=[question['start']],target_nodes=[question['target']])
            else:name,args='get_code_neighbors',dict(node=question['start'])
        elif count==1:
            assert len(body['messages'])==4
            name,args='execute_python',{'code':'assert set(question)=={"query_id","start","target","max_hops"}; assert len(observations)==1; assert set(observations[0])=={"name","arguments","result"}; print("observations-only")'}
        else:
            assert len(body['messages'])==6
            result=json.loads(body['messages'][3]['content'])['result']
            if arm=='all-paths-tool-agent':
                paths=[[result['nodes'][i] for i in p['nodes']] for p in result['pairs'][0]['paths']]
                complete=result['complete']
            else:
                paths=[[question['start'],question['target']]] if any(n.rsplit(';',1)[-1]==question['target'] for n in result['neighbors']) else []
                complete=False
            name,args='submit_graph_answer',dict(paths=paths,complete=complete)
        count+=1
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
            {'id':str(count),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    connection=dict(enabled=True,base_url='https://example.invalid/v1',model='scripted-engineering-smoke',
                    api_key_env='GRAPH_STUDY_SMOKE_KEY',protocol='chat_completions',stream=False)
    transport=ChatTransport(connection,client)
    try:
        row=run_episode(question,arm,make_backend(arm,reference),DockerPython(image,docker_host=host),transport,
                        Path(root)/(question['query_id']+'--'+arm),effective_config(image))
        assert row['stop_reason']=='submitted'
        assert row['counters']['model_rounds']==3 and row['counters']['python_attempts']==1
        assert row['usage']['unknown_usage_responses']==3
        python_result=next(t['result'] for t in row['traces'] if t['name']=='execute_python')
        assert python_result['status']=='ok' and python_result['cleanup']=='confirmed'
        evaluate(row,reference)
        return dict(configuration=LABELS[arm],rounds=3,graph_attempts=1,python_attempts=1,
                    formal_submission=True,prompt_and_schema_verified=True,usage_unknown=True,
                    real_model_requests=0)
    finally:transport.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--graph-dir',required=True)
    p.add_argument('--python-image',required=True);p.add_argument('--docker-host')
    p.add_argument('--run-name');p.add_argument('--runs-dir',default='runs');args=p.parse_args(argv)
    questions,refs=load_inputs(args.graph_dir);q=questions[0]
    def exercise(tmp):
        save(Path(tmp)/'manifest.json',dict(mode='mock',real_model_requests=0,planned_tasks=150,scripted_tasks=3))
        jobs=[(q,refs[q['query_id']],arm,tmp,args.python_image,args.docker_host) for arm in LABELS]
        with ProcessPoolExecutor(max_workers=3,mp_context=multiprocessing.get_context('spawn'),max_tasks_per_child=1) as pool:
            return list(pool.map(smoke_worker,jobs))
    if args.run_name:
        out=run_path(args.run_name,args.runs_dir);out.mkdir(parents=True,exist_ok=False);results=exercise(out)
    else:
        with tempfile.TemporaryDirectory(prefix='benchmark-smoke-') as tmp:results=exercise(tmp)
    print(json.dumps(dict(kind='engineering_smoke',configurations=results,real_model_requests=0),indent=2))
if __name__=='__main__':main()
