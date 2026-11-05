"""Serial, evidence-only agent loop. No scoring keys or full graph enter its inputs."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import time

from python_tool import fit_output_prefix
from support import CONFIG, HERE, argument_error, save, tool_definitions


GRAPH_TOOLS={'get_code_neighbors','get_code_subgraph','graph_find_paths'}
TOOL_BUDGETS={
    **{name: ('graph_attempts', 'max_graph_queries') for name in GRAPH_TOOLS},
    'execute_python': ('python_attempts', 'max_python_calls'),
    'submit_graph_answer': ('submit_attempts', 'max_submissions'),
}


def inspect_calls(calls):
    ids=Counter(call['id'] for call in calls if isinstance(call,dict) and isinstance(call.get('id'),str))
    inspected=[]
    for call in calls:
        envelope=call if isinstance(call,dict) else {}
        function=envelope.get('function')
        function=function if isinstance(function,dict) else {}
        call_id=envelope.get('id')
        name=function.get('name')
        arguments=function.get('arguments')
        errors=[]
        if (not isinstance(call_id,str) or not call_id or envelope.get('type')!='function'
                or not isinstance(name,str) or not name or not isinstance(arguments,str)):
            errors.append('malformed_function_envelope')
        if isinstance(call_id,str) and ids[call_id]>1:
            errors.append('duplicate_call_id')
        inspected.append((call_id,name,arguments,errors))
    return inspected


def run_episode(question,arm,backend,python_tool,transport,out,config=None):
    cfg=deepcopy(config or CONFIG)
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();deadline=started+cfg['max_episode_seconds']
    prompt=(HERE/'config/system-prompt.txt').read_text()
    if cfg['arms'][arm]['guidance']:
        prompt+='\n\n'+(HERE/'config/search-guidance.md').read_text()
    messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(question)}]
    counters=dict(graph_emitted=0,graph_attempts=0,graph_native_executions=0,
        python_emitted=0,python_attempts=0,submit_attempts=0,tool_calls_emitted=0,
        suppressed=0,invalid_tools=0,model_rounds=0,delivered_characters=0)
    usage=dict(prompt_tokens=0,completion_tokens=0,unknown_usage_responses=0)
    observations=[];traces=[];answer=None;submitted_answer=None;submitted_answer_parsed=False;reason='model_round_limit';error=None
    allowed={t['function']['name'] for t in tool_definitions(arm)}
    save(out/'public_question.json',question)

    def remaining(): return max(0,deadline-time.monotonic())

    def response_packet(result):
        packet={'result':result,'remaining':{
            'graph_queries':max(0,cfg['max_graph_queries']-counters['graph_attempts']),
            'python_calls':max(0,cfg['max_python_calls']-counters['python_attempts']),
            'submissions':max(0,1-counters['submit_attempts'])}}
        return json.dumps(packet)

    try:
        for round_number in range(1,cfg['max_model_rounds']+1):
            if not remaining(): reason='episode_time_limit';break
            round_dir=out/'rounds'/f'{round_number:03d}';round_dir.mkdir(parents=True)
            schemas=[]
            for tool in tool_definitions(arm):
                name=tool['function']['name']
                if name in GRAPH_TOOLS and counters['graph_attempts']>=cfg['max_graph_queries']:continue
                if name=='execute_python' and counters['python_attempts']>=cfg['max_python_calls']:continue
                schemas.append(tool)
            req={'messages':deepcopy(messages),'tools':schemas,'tool_choice':'auto',
                 'temperature':cfg['temperature'],'max_tokens':cfg['max_output_tokens'],'stream':False}
            save(round_dir/'request.json',req);counters['model_rounds']+=1
            model_start=time.monotonic()
            response=None
            try:
                try:
                    response=transport(req,round_dir,remaining())
                finally:
                    u=response.get('usage') if isinstance(response,dict) else None
                    if (isinstance(u,dict) and type(u.get('prompt_tokens')) is int
                            and type(u.get('completion_tokens')) is int
                            and u['prompt_tokens']>=0 and u['completion_tokens']>=0):
                        usage['prompt_tokens']+=u['prompt_tokens']
                        usage['completion_tokens']+=u['completion_tokens']
                    else:
                        usage['unknown_usage_responses']+=1
            except Exception as exc:
                error={'type':type(exc).__name__,'message':str(exc)}
                save(round_dir/'error.json',error);reason='model_error';break
            finally:
                save(round_dir/'timing.json',{'elapsed_seconds':time.monotonic()-model_start})
            save(round_dir/'response.json',response)
            try:
                original=response['choices'][0]['message']
                calls=original.get('tool_calls')
                if calls is None:calls=[]
                if not isinstance(calls,list):raise ValueError('Tool calls not a list')
                inspected=inspect_calls(calls)
                counters['tool_calls_emitted']+=len(calls)
                counters['graph_emitted']+=sum(isinstance(name,str) and name in GRAPH_TOOLS for _,name,_,_ in inspected)
                counters['python_emitted']+=sum(name=='execute_python' for _,name,_,_ in inspected)
                role_invalid=original.get('role')!='assistant'
                if role_invalid or any(errors for _,_,_,errors in inspected):
                    for call_id,name,raw_args,errors in inspected:
                        if role_invalid:errors.append('invalid_message_role')
                        trace={'round':round_number,'id':call_id,'name':name,'arguments_raw':raw_args,
                               'executed':False,'budget_admitted':False,'envelope_errors':errors}
                        if errors and isinstance(name,str) and name in allowed and remaining():
                            category,cap_key=TOOL_BUDGETS[name]
                            if counters[category]<cfg[cap_key]:
                                counters[category]+=1;trace['budget_admitted']=True
                            else:trace['suppressed_reason']='tool_budget_exhausted'
                        else:
                            trace['suppressed_reason']='malformed_response'
                            if isinstance(name,str) and name not in allowed:counters['invalid_tools']+=1
                        if 'suppressed_reason' in trace:counters['suppressed']+=1
                        traces.append(trace)
                    save(round_dir/'tool-traces.json',[t for t in traces if t['round']==round_number])
                    raise ValueError('Expected assistant message' if role_invalid else 'Malformed function call envelope')
                message={'role':'assistant','content':original.get('content')}
                if calls:message['tool_calls']=deepcopy(calls)
                messages.append(message)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
                error={'type':type(exc).__name__,'message':str(exc)}
                save(round_dir/'error.json',error);reason='model_format_error';break
            if not remaining():reason='episode_time_limit';break
            if not calls:reason='no_submission';break
            stop=False
            for call in calls:
                name=call['function']['name'];raw_args=call['function']['arguments']
                trace={'round':round_number,'id':call['id'],'name':name,'arguments_raw':raw_args,'executed':False}
                if stop:
                    counters['suppressed']+=1;trace['suppressed_reason']='after_terminal_tool';traces.append(trace);continue
                result=None;args=None;category=None;admitted=False
                if not remaining():
                    result={'status':'error','error_type':'episode_time_limit'};stop=True;reason='episode_time_limit'
                elif name not in allowed:
                    counters['invalid_tools']+=1;result={'status':'error','error_type':'tool_not_available'}
                else:
                    category,cap_key=TOOL_BUDGETS[name]
                    if counters[category]>=cfg[cap_key]:
                        counters['suppressed']+=1;result={'status':'error','error_type':'tool_budget_exhausted'}
                    else:
                        admitted=True;counters[category]+=1
                        try:args=json.loads(raw_args)
                        except (ValueError,RecursionError):result={'status':'error','error_type':'invalid_json'}
                        if result is None:
                            if name=='submit_graph_answer':
                                submitted_answer=deepcopy(args);submitted_answer_parsed=True
                            invalid=argument_error(name,args)
                            if invalid:result={'status':'error','error_type':'invalid_arguments','message':invalid}
                trace['arguments']=args;trace['budget_admitted']=admitted
                if result is None:
                    trace['executed']=True
                    try:
                        if name in GRAPH_TOOLS:
                            counters['graph_native_executions']+=1
                            result=backend.query(name,args)
                        elif name=='execute_python':
                            result=python_tool.execute(args['code'],deepcopy(question),deepcopy(observations),remaining_seconds=remaining())
                        else:result={'status':'ok','accepted':True}
                    except Exception as exc:
                        result={'status':'error','error_type':type(exc).__name__,'message':str(exc)}
                trace['raw_result']=deepcopy(result)
                if name=='execute_python' and result.get('cleanup')=='unknown':
                    stop=True;reason='python_cleanup_unconfirmed'
                if not remaining():
                    result={'status':'error','error_type':'episode_time_limit'};stop=True;reason='episode_time_limit'
                text=response_packet(result)
                result_limit=cfg['python_output_chars'] if name=='execute_python' else cfg['max_result_chars']
                if len(text)>result_limit and name=='execute_python':
                    overhead=len(text)-len(json.dumps(result))
                    bounded={**result,'output_truncated':True}
                    bounded.setdefault('original_status',result.get('status'))
                    result=fit_output_prefix(bounded,result_limit-overhead)
                    text=response_packet(result)
                if len(text)>result_limit:
                    result={'status':'error','error_type':'output_limit'};text=response_packet(result)
                if counters['delivered_characters']+len(text)>cfg['max_total_result_chars']:
                    trace['suppressed_reason']='total_output_limit';traces.append(trace)
                    reason='total_output_limit';stop=True;break
                counters['delivered_characters']+=len(text)
                trace['result']=deepcopy(result);trace['delivered_text']=text
                if name in GRAPH_TOOLS and admitted:
                    observations.append({'name':name,'arguments':args,'result':deepcopy(result)})
                messages.append({'role':'tool','tool_call_id':call['id'],'content':text})
                traces.append(trace)
                if name=='submit_graph_answer' and admitted:
                    stop=True
                    if result.get('accepted') is True and remaining():answer=deepcopy(args);reason='submitted'
                    elif reason!='episode_time_limit':reason='invalid_submission'
            save(round_dir/'tool-traces.json',[t for t in traces if t['round']==round_number])
            if stop:break
    except BaseException as exc:
        error={'type':type(exc).__name__,'message':str(exc)};reason='interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'runner_error'
        if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
    finally:
        row={'query_id':question['query_id'],'arm':arm,'label':cfg['arms'][arm]['label'],
             'public_question':question,'answer':answer,'submitted_answer':submitted_answer,
             'submitted_answer_parsed':submitted_answer_parsed,'stop_reason':reason,'error':error,
             'counters':counters,'usage':usage,'observations':observations,'traces':traces}
        save(out/'messages.json',messages);save(out/'result.json',row)
    return row
