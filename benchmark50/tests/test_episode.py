import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from benchmark50.runner import episode
from benchmark50.support import CONFIG


def call(name,args,id='call1'):
    return {'id':id,'type':'function','function':{'name':name,'arguments':args if isinstance(args,str) else json.dumps(args)}}


def response(*calls):
    return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':list(calls)},'finish_reason':'tool_calls'}],
            'usage':{'prompt_tokens':10,'completion_tokens':5}}


class Backend:
    def __init__(self): self.calls=[]
    def query(self,name,args):
        self.calls.append((name,args))
        node=args['node'];other='t' if node=='s' else 's'
        return {'status':'ok','node':node,'neighbors':[other],'count':1,'is_truncated':False}


class Python:
    def __init__(self): self.inputs=[]
    def execute(self,code,question,observations,remaining_seconds=600):
        self.inputs.append(copy.deepcopy((code,question,observations)))
        return {'status':'ok','stdout':'done','stderr':''}


class EpisodeTests(unittest.TestCase):
    def run_script(self,script,arm='legacy-tools-agent',config=None):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        backend=Backend();python=Python();requests=[]
        def transport(req,out,remaining):
            requests.append(copy.deepcopy(req))
            item=script[len(requests)-1]
            if isinstance(item,Exception): raise item
            return item
        row=episode.run_episode(dict(query_id='q',start='s',target='t',max_hops=4),arm,
            backend,python,transport,Path(self.tmp.name)/'episode',config or CONFIG)
        return row,backend,python,requests

    def test_actual_tools_then_python_then_submission_no_hidden_backend_calls(self):
        r,b,p,req=self.run_script([
            response(call('get_code_neighbors',{'node':'s'})),
            response(call('execute_python',{'code':'print(observations)'})),
            response(call('submit_graph_answer',{'paths':[['s','t']],'complete':True}))])
        self.assertEqual(len(b.calls),1)
        self.assertEqual(r['counters']['python_attempts'],1)
        self.assertEqual(r['answer']['paths'],[['s','t']])
        self.assertEqual(p.inputs[0][1],dict(query_id='q',start='s',target='t',max_hops=4))
        self.assertEqual(len(p.inputs[0][2]),1)
        self.assertEqual(r['usage']['prompt_tokens'],30)

    def test_invalid_json_and_schema_each_consume_one_graph_attempt(self):
        r,b,_,_=self.run_script([response(call('get_code_neighbors','{'),
            call('get_code_neighbors',{'node':'s','hidden':True},'call2'),
            call('submit_graph_answer',{'paths':[],'complete':False},'call3'))])
        self.assertEqual(r['counters']['graph_attempts'],2)
        self.assertEqual(b.calls,[])

    def test_32_query_cap_and_overemission(self):
        calls=[call('get_code_neighbors',{'node':'s'},f'c{i}') for i in range(33)]
        calls += [call('submit_graph_answer',{'paths':[['s','t']],'complete':True},'last')]
        r,b,_,_=self.run_script([response(*calls)])
        self.assertEqual(len(b.calls),32)
        self.assertEqual(r['counters']['graph_attempts'],32)
        self.assertEqual(r['counters']['graph_emitted'],33)
        self.assertEqual(r['counters']['suppressed'],1)

    def test_calls_after_submit_never_execute(self):
        r,b,_,_=self.run_script([response(call('submit_graph_answer',{'paths':[],'complete':False}),
            call('get_code_neighbors',{'node':'s'},'c2'))])
        self.assertEqual(b.calls,[])
        self.assertEqual(r['counters']['suppressed'],1)

    def test_model_error_is_preserved_without_retry(self):
        r,b,_,req=self.run_script([RuntimeError('server unavailable')])
        self.assertEqual(len(req),1)
        self.assertEqual(r['stop_reason'],'model_error')
        self.assertEqual(r['usage']['unknown_usage_responses'],1)
        self.assertIsNone(r['answer'])
        self.assertTrue((Path(self.tmp.name)/'episode/rounds/001/error.json').exists())

    def test_guidance_is_only_arm_prompt_difference(self):
        _,_,_,plain=self.run_script([response(call('submit_graph_answer',{'paths':[],'complete':False}))])
        _,_,_,guided=self.run_script([response(call('submit_graph_answer',{'paths':[],'complete':False}))],arm='guided-legacy-tools-agent')
        self.assertEqual(plain[0]['tools'],guided[0]['tools'])
        self.assertTrue(guided[0]['messages'][0]['content'].startswith(plain[0]['messages'][0]['content']))

    def test_unavailable_tool_never_executes(self):
        r,b,_,_=self.run_script([response(call('get_code_neighbors',{'node':'s'}),
            call('submit_graph_answer',{'paths':[],'complete':False},'s'))],arm='all-paths-tool-agent')
        self.assertEqual(b.calls,[])
        self.assertEqual(r['counters']['invalid_tools'],1)

    def test_graph_response_cannot_leak_via_python_if_not_delivered(self):
        config=copy.deepcopy(CONFIG);config['max_result_chars']=250
        # Oversized node ID is valid input, but its oversized returned packet is rejected.
        r,b,p,_=self.run_script([response(call('get_code_neighbors',{'node':'x'*1000})),
            response(call('execute_python',{'code':'print(observations)'})),
            response(call('submit_graph_answer',{'paths':[],'complete':False}))],config=config)
        self.assertEqual(p.inputs[0][2][0]['result']['status'],'error')
        self.assertNotIn('neighbors',p.inputs[0][2][0]['result'])

    def test_unconfirmed_python_cleanup_stops_before_next_model_round(self):
        from unittest.mock import patch
        with patch.object(Python,'execute',return_value={'status':'timeout','cleanup':'unknown','container_name':'owned-x'}):
            r,_,_,req=self.run_script([response(call('execute_python',{'code':'while True: pass'}))])
        self.assertEqual(r['stop_reason'],'python_cleanup_unconfirmed')
        self.assertEqual(len(req),1)


    def test_16_python_attempt_cap_and_overemission(self):
        calls = [call('execute_python', {'code':'print(1)'}, f'p{i}') for i in range(17)]
        calls.append(call('submit_graph_answer', {'paths':[], 'complete':False}, 'submit'))
        row, _, python, _ = self.run_script([response(*calls)])
        self.assertEqual(len(python.inputs), 16)
        self.assertEqual(row['counters']['python_attempts'], 16)
        self.assertEqual(row['counters']['python_emitted'], 17)
        self.assertEqual(row['counters']['suppressed'], 1)

    def test_model_round_limit_and_deadline_stop_new_requests(self):
        cfg = copy.deepcopy(CONFIG)
        cfg['max_model_rounds'] = 2
        row, backend, _, requests = self.run_script([
            response(call('get_code_neighbors', {'node':'s'})),
            response(call('get_code_neighbors', {'node':'t'})),
        ], config=cfg)
        self.assertEqual(len(requests), 2)
        self.assertEqual(row['stop_reason'], 'model_round_limit')
        cfg['max_episode_seconds'] = 0
        row, backend, _, requests = self.run_script([], config=cfg)
        self.assertEqual(requests, [])
        self.assertEqual(backend.calls, [])
        self.assertIsNone(row['answer'])


if __name__=='__main__': unittest.main()
