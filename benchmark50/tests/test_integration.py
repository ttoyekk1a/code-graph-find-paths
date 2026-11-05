"""Scripted integration (not an LLM) with actual graph tools and isolated Python."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from benchmark50.runner.episode import run_episode
from benchmark50.runner.python_tool import DockerPython
from benchmark50.support import CONFIG,grade_episode,load_inputs,make_backend

LEGACY_CODE='''
from collections import defaultdict
a=defaultdict(set)
for obs in observations:
 r=obs['result']
 if obs['name']=='get_code_neighbors' and r.get('status')=='ok':
  u=r['node'].rsplit(';',1)[-1]
  for v in r['neighbors']:
   v=v.rsplit(';',1)[-1];a[u].add(v);a[v].add(u)
paths=[]
def visit(p):
 if p[-1]==question['target']:paths.append(p);return
 if len(p)-1>=question['max_hops']:return
 for v in sorted(a[p[-1]]):
  if v not in p:visit(p+[v])
visit([question['start']])
import json
import os
print(json.dumps(paths))
'''
CANDIDATE_CODE='''
import json
import os
r=observations[0]['result']
print(json.dumps([[r['nodes'][i] for i in p['nodes']] for p in r['pairs'][0]['paths']]))
'''


def make_response(name,args,num):
    return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
        {'id':f'c{num}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]},'finish_reason':'tool_calls'}]}


class ScriptedDriver:
    """Public-question/visible-result-only state machine, never an efficacy result."""
    def __init__(self,q,arm):
        self.q=q;self.arm=arm;self.n=0;self.queue=[q['start'],q['target']];self.seen=set()

    def __call__(self,request,out,remaining):
        self.n+=1
        tools=[m for m in request['messages'] if m['role']=='tool']
        last=json.loads(tools[-1]['content'])['result'] if tools else None
        if last and 'stdout' in last:
            if last['status']!='ok':raise RuntimeError('Python integration failed: '+str(last))
            return make_response('submit_graph_answer',{'paths':json.loads(last['stdout']),'complete':True},self.n)
        if self.arm=='all-paths-tool-agent':
            if not tools:return make_response('graph_find_paths',{'source_nodes':[self.q['start']], 'target_nodes':[self.q['target']], 'max_hops':4},self.n)
            return make_response('execute_python',{'code':CANDIDATE_CODE},self.n)
        if last and last.get('node') in {self.q['start'],self.q['target']}:
            self.queue.extend(v.rsplit(';',1)[-1] for v in last['neighbors'])
        while self.queue:
            n=self.queue.pop(0)
            if n in self.seen:continue
            self.seen.add(n);return make_response('get_code_neighbors',{'node':n,'max_neighbors':20000},self.n)
        return make_response('execute_python',{'code':LEGACY_CODE},self.n)


@unittest.skipUnless(os.environ.get('BENCHMARK50_TEST_DOCKER') == '1' and os.environ.get('BENCHMARK50_GRAPHS_DIR'), 'Requires explicit Docker and pinned graph integration settings')
class IntegrationTests(unittest.TestCase):
    def test_real_22_path_graph_all_three_arms_with_python(self):
        questions,refs=load_inputs(Path(os.environ['BENCHMARK50_GRAPHS_DIR']));q=next(q for q in questions if q['query_id']=='expanded-015')
        py=DockerPython(os.environ.get('BENCHMARK50_TEST_IMAGE', CONFIG['python_image']),
            docker_host=os.environ.get('BENCHMARK50_DOCKER_HOST'),
            docker_config=os.environ.get('BENCHMARK50_DOCKER_CONFIG'))
        with tempfile.TemporaryDirectory() as td:
            for arm in CONFIG['arms']:
                r=run_episode(q,arm,make_backend(arm,refs[q['query_id']]),py,ScriptedDriver(q,arm),Path(td)/arm)
                g=grade_episode(q,r['answer'],refs[q['query_id']]['expected_paths'],r['observations'])
                self.assertTrue(g['exact_set_success'],(arm,r['stop_reason']))
                self.assertEqual(g['matched_path_count'],22)
                self.assertEqual(r['counters']['python_attempts'],1)
                self.assertEqual(r['counters']['graph_native_executions'],1 if arm=='all-paths-tool-agent' else 4)


if __name__=='__main__':unittest.main()
