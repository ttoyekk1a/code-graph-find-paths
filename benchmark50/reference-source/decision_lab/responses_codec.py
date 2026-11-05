"""Offline text/function-only full-history codec. No transport or retry behavior.

Reasoning items are audit-only, matching the harness policy; this is not a
lossless replay codec for models requiring opaque reasoning state.
"""
from copy import deepcopy
from collections.abc import Mapping


class CodecError(ValueError):
    """Input cannot be represented by this deliberately narrow codec."""


class ResponseStateError(CodecError):
    """Response is unsuccessful, nonterminal, or has unsupported completion state."""


def _keys(value, allowed, context):
    if not isinstance(value, Mapping):
        raise CodecError(f'{context}: expected mapping')
    extra = set(value) - set(allowed)
    if extra:
        raise CodecError(f'{context}: unsupported fields {sorted(extra)}')


def _string(value, context):
    if not isinstance(value, str):
        raise CodecError(f'{context}: expected string')
    return value


def build_response_request(chat_request, model):
    """Convert a text Chat request without changing source strings or call IDs."""
    _keys(chat_request, {'messages','model','tools','tool_choice','temperature','max_tokens','stream'}, 'request')
    _string(model, 'model')
    if chat_request.get('model', model) != model:
        raise CodecError('model mismatch')
    if chat_request.get('stream', False) is not False:
        raise CodecError('streaming unsupported')
    if not isinstance(chat_request.get('messages'), list):
        raise CodecError('messages: expected list')
    items = []
    for message in chat_request['messages']:
        role = message.get('role') if isinstance(message, Mapping) else None
        allowed = {'role','content'}
        if role == 'assistant': allowed |= {'tool_calls'}
        if role == 'tool': allowed |= {'tool_call_id'}
        _keys(message, allowed, 'message')
        if role == 'tool':
            items.append({'type':'function_call_output', 'call_id':_string(message.get('tool_call_id'),'tool_call_id'), 'output':_string(message.get('content'),'tool output')})
            continue
        if role not in {'system','developer','user','assistant'}:
            raise CodecError(f'unsupported role {role!r}')
        content = message.get('content')
        calls = message.get('tool_calls', [])
        if content is not None:
            items.append({'type':'message','role':role,'content':_string(content,'content')})
        elif role != 'assistant' or not calls:
            raise CodecError('null content requires assistant tool calls')
        if not isinstance(calls,list): raise CodecError('tool_calls: expected list')
        for call in calls:
            _keys(call, {'id','type','function'}, 'tool call')
            if call.get('type') != 'function': raise CodecError('only function tool calls supported')
            fn = call.get('function')
            _keys(fn, {'name','arguments'}, 'function call')
            items.append({'type':'function_call','call_id':_string(call.get('id'),'call id'),'name':_string(fn.get('name'),'function name'),'arguments':_string(fn.get('arguments'),'arguments')})
    result = {'model':model,'input':items,'background':True,'store':True,'stream':False}
    if 'tools' in chat_request:
        if not isinstance(chat_request['tools'],list): raise CodecError('tools: expected list')
        result['tools'] = []
        for tool in chat_request['tools']:
            _keys(tool, {'type','function'}, 'tool')
            if tool.get('type') != 'function': raise CodecError('only function tools supported')
            fn = tool.get('function')
            _keys(fn, {'name','description','parameters','strict'}, 'function definition')
            _string(fn.get('name'), 'function name')
            if 'description' in fn: _string(fn['description'],'description')
            if 'parameters' in fn and not isinstance(fn['parameters'],Mapping): raise CodecError('parameters: expected schema mapping')
            # Responses may normalize an omitted strict field; explicit False
            # preserves Chat's non-strict default without changing the schema.
            result['tools'].append({'type':'function', **deepcopy(fn), 'strict':fn.get('strict',False)})
    choice = chat_request.get('tool_choice','auto')
    if isinstance(choice, Mapping):
        _keys(choice, {'type','function'}, 'tool_choice')
        if choice.get('type') != 'function': raise CodecError('unsupported tool_choice')
        _keys(choice.get('function'), {'name'}, 'tool_choice function')
        choice = {'type':'function','name':_string(choice['function'].get('name'),'tool_choice name')}
    elif choice not in ('auto','none','required'):
        raise CodecError('unsupported tool_choice')
    result['tool_choice'] = deepcopy(choice)
    for source,target in [('max_tokens','max_output_tokens'),('temperature','temperature')]:
        if source in chat_request: result[target] = deepcopy(chat_request[source])
    return result


def response_to_chat(response):
    """Map a terminal Responses dictionary into the harness's Chat shape.

    Unknown usage fields and the entire raw response are retained for audit.
    Output text is concatenated in output order; function-call order is retained.
    """
    if not isinstance(response, Mapping): raise CodecError('response: expected mapping')
    status = response.get('status')
    if status == 'completed': finish = 'stop'
    elif status == 'incomplete':
        reason = (response.get('incomplete_details') or {}).get('reason')
        if reason not in {'max_output_tokens','content_filter'}:
            raise ResponseStateError(f'unsupported incomplete reason {reason!r}')
        finish = {'max_output_tokens':'length','content_filter':'content_filter'}[reason]
    else:
        raise ResponseStateError(f'response status {status!r}: {response.get("error")!r}')
    texts, refusals, calls = [], [], []
    if not isinstance(response.get('output'), list): raise CodecError('output: expected list')
    for item in response['output']:
        kind = item.get('type')
        if kind == 'reasoning': continue
        if kind == 'function_call':
            calls.append({'id':_string(item.get('call_id'),'call_id'),'type':'function','function':{'name':_string(item.get('name'),'name'),'arguments':_string(item.get('arguments'),'arguments')}})
        elif kind == 'message':
            if item.get('role') != 'assistant': raise CodecError('non-assistant output message')
            for part in item.get('content',[]):
                if part.get('type') == 'output_text': texts.append(_string(part.get('text'),'output text'))
                elif part.get('type') == 'refusal': refusals.append(_string(part.get('refusal'),'refusal'))
                else: raise CodecError(f'unsupported content {part.get("type")!r}')
        else: raise CodecError(f'unsupported output item {kind!r}')
    message = {'role':'assistant','content':''.join(texts) if texts else None}
    if calls:
        message['tool_calls'] = calls
        if status == 'completed': finish = 'tool_calls'
    if refusals: message['refusal'] = ''.join(refusals)
    usage = deepcopy(response.get('usage'))
    if usage is not None:
        if not isinstance(usage, Mapping): raise CodecError('usage: expected mapping')
        for source,target in [('input_tokens','prompt_tokens'),('output_tokens','completion_tokens'),('input_tokens_details','prompt_tokens_details'),('output_tokens_details','completion_tokens_details')]:
            if source in usage:
                if target in usage: raise CodecError(f'usage alias collision: {target}')
                usage[target] = usage.pop(source)
    return {'id':response.get('id'),'object':'chat.completion','model':response.get('model'),'choices':[{'index':0,'message':message,'finish_reason':finish}],'usage':usage,'_raw_response':deepcopy(response)}
