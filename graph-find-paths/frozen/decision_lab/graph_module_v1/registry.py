"""Shared actual-tool registry. No transport, controller, scoring or launch.

assets has exactly graph_path, graph_sha256, embedding_path, embedding_sha256,
source_manifest (approved generic binding manifest), workspace_root. Both ctx
graph_dir and embeddings_dir must name those exact files. Runtime identity is
the actual approved identity/ownership shape, never fabricated here.

Wrapper policy: json.dumps defaults, unchanged result when <=8000 characters.
For larger dicts remove trailing WHOLE records in top-level results/neighbors/
nodes/edges, then shorten suffixes only of content/stdout/stderr/error_message.
Original numerical fields remain unchanged; explicit omission counts, original
serialized size and is_truncated=True are added. Never trim native witness
internals or identifiers. Unsupported shapes return OutputLimitExceeded with
unknown internal work and audit-only raw retention. No reranking or relevance.

Audit snapshots retain original call/result separately. observations contains
ONLY exact delivered JSON plus call identity/arguments/runtime; getters return
defensive copies, so later mutations cannot rewrite citation history.
Deep JSON values use {audit_representation: lossless-json-v1, serialized_json:
<default-json text>} in retained fields. This is audit data, never executable
arguments or additional tool evidence. Unsupported Python objects are identified
without retaining references or calling user copy/repr hooks.
"""
from copy import deepcopy
import inspect
import json
from pathlib import Path
from time import perf_counter
import types
import typing

from google.adk.tools import FunctionTool
from swegemma.tools.base import parse_tool_response
from swegemma.tools.graph import make_get_code_neighbors,make_search_similar_code,make_get_code_subgraph
from swegemma.tools.workspace import make_read_file
from swegemma.tools.execution import make_run_command
from decision_lab.graph_native_v1.native import make_graph_find_paths,make_graph_compare_locations
from decision_lab.graph_native_v1.compact import CompactNativeGraphWitnessSession as NativeGraphWitnessSession
from decision_lab.graph_native_v1.binding import validate_manifest as validate_binding
from decision_lab.graph_native_v1.source_read import HARD_LIMITS,RuntimeSnapshotReader,validate_runtime
from .public import _read_checked

OUTPUT_LIMIT=8000
WRAPPER_POLICY='trailing-top-level-records-then-payload-string-suffix-v1'


def _serialized_snapshot(value):
    """Iterative exact JSON value serialization, without arbitrary object hooks.

    Return default json.dumps text and depth; no recursive parser/copy is needed
    to retain a malformed call. Active-container tracking rejects cycles while
    permitting repeated references with the usual JSON value semantics.
    """
    chunks=[];active=set();depth=0;pending=[('value',value,0)]
    while pending:
        action,item,level=pending.pop()
        if action=='text':chunks.append(item);continue
        if action=='leave':active.remove(item);continue
        kind=type(item);depth=max(depth,level)
        if kind in (str,int,float,bool,type(None)):
            chunks.append(json.dumps(item));continue
        if kind not in (dict,list):raise ValueError('UnsupportedPythonType:'+kind.__name__)
        if id(item) in active:raise ValueError('CircularPythonInput')
        active.add(id(item));pending.append(('leave',id(item),level))
        if kind is dict:
            if any(type(key) is not str for key in item):raise ValueError('NonStringPythonKey')
            entries=list(item.items());chunks.append('{');pending.append(('text','}',level))
            for index in range(len(entries)-1,-1,-1):
                key,child=entries[index]
                pending.append(('value',child,level+1));pending.append(('text',json.dumps(key)+': ',level))
                if index:pending.append(('text',', ',level))
        else:
            chunks.append('[');pending.append(('text',']',level))
            for index in range(len(item)-1,-1,-1):
                pending.append(('value',item[index],level+1))
                if index:pending.append(('text',', ',level))
    return ''.join(chunks),depth


def _snapshot(value):
    """Safe retained copy; serialized deep JSON remains shallow on every read."""
    try:serialized,depth=_serialized_snapshot(value)
    except (ValueError,TypeError,OverflowError) as exc:
        return dict(audit_representation='unsupported-python-input-v1',
                    python_type=type(value).__name__,reason=str(exc),lossless=False)
    # Reserve ample nesting room for every enclosing ledger/record/getter copy.
    if depth<=64:return deepcopy(value)
    return dict(audit_representation='lossless-json-v1',serialized_json=serialized)


def _fallback(original_size, reason='OutputLimitExceeded'):
    return dict(status='error',error_type=reason,is_truncated=True,
        output_omissions=dict(policy=WRAPPER_POLICY,original_characters=original_size,result_omitted=True),
        accounting=dict(tool_internal_work=None,internal_work_status='unknown_see_unmodified_audit'))


def bound_result(raw):
    """Deterministic public cap; never mutates raw or numerical fields."""
    try:
        json.dumps(raw,allow_nan=False)
        original=len(json.dumps(raw))
    except (ValueError,TypeError,OverflowError,RecursionError):return _fallback(None,'InvalidToolResult')
    if original<=OUTPUT_LIMIT:
        try:return deepcopy(raw)
        except RecursionError:return _fallback(original,'InvalidToolResult')
    if type(raw) is not dict:return _fallback(original)
    try:result=deepcopy(raw)
    except RecursionError:return _fallback(original,'InvalidToolResult')
    # Existing metadata names cannot be silently overwritten.
    if 'output_omissions' in result:return _fallback(original)
    omit=dict(policy=WRAPPER_POLICY,original_characters=original,records={},string_characters={},numerical_fields='unchanged_original_semantics')
    result.update(is_truncated=True,output_omissions=omit)
    for key in reversed(list(raw)):
        if key not in ('results','neighbors','nodes','edges') or type(result[key]) is not list:continue
        while result[key] and len(json.dumps(result))>OUTPUT_LIMIT:
            result[key].pop();omit['records'][key]=omit['records'].get(key,0)+1
        if len(json.dumps(result))<=OUTPUT_LIMIT:return result
    for key in reversed(list(raw)):
        if key not in ('content','stdout','stderr','error_message') or type(result[key]) is not str:continue
        value=result[key];low=0;high=len(value)
        omit['string_characters'][key]=len(value)
        result[key]=''
        if len(json.dumps(result))>OUTPUT_LIMIT:continue
        while low<high:
            middle=(low+high+1)//2;result[key]=value[:middle];omit['string_characters'][key]=len(value)-middle
            if len(json.dumps(result))<=OUTPUT_LIMIT:low=middle
            else:high=middle-1
        result[key]=value[:low];omit['string_characters'][key]=len(value)-low
        if len(json.dumps(result))<=OUTPUT_LIMIT:return result
    return _fallback(original)


def _normalize(value):
    if type(value) is list:return [_normalize(x) for x in value]
    if type(value) is not dict:return value
    return {key:(val.lower() if key=='type' and type(val) is str else _normalize(val)) for key,val in value.items()}


def _schema(fn):
    declaration=FunctionTool(fn)._get_declaration().model_dump(mode='json',exclude_none=True,by_alias=True)
    if declaration.get('name')!=fn.__name__:raise ValueError('ADKDeclarationNameMismatch')
    params=declaration.get('parameters',declaration.get('parametersJsonSchema'))
    if type(params) is not dict:raise ValueError('MissingADKParameters')
    return dict(type='function',function=dict(name=declaration['name'],description=declaration.get('description',''),parameters=_normalize(params)))


def _matches(value, annotation):
    if annotation is inspect.Signature.empty:return True
    origin=typing.get_origin(annotation);args=typing.get_args(annotation)
    if origin in (typing.Union,types.UnionType):return any(_matches(value,a) for a in args)
    if origin is list:return type(value) is list and all(_matches(x,args[0]) for x in value)
    if annotation is type(None):return value is None
    if annotation in (str,int,bool,dict,list):return type(value) is annotation
    raise ValueError('UnsupportedToolAnnotation')


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('DuplicateArgumentKey')
        result[key]=value
    return result


def _parse_call(call,tools):
    if (type(call) is not dict or set(call)!={'id','type','function'} or type(call['id']) is not str or not call['id'] or call['type']!='function'):
        raise ValueError('InvalidCallEnvelope')
    fn=call['function']
    if type(fn) is not dict or set(fn)!={'name','arguments'} or type(fn['name']) is not str:raise ValueError('InvalidFunctionEnvelope')
    name=fn['name']
    if name not in tools:raise ValueError('UnknownTool')
    arguments=fn['arguments']
    if type(arguments) is str:
        try:arguments=json.loads(arguments,object_pairs_hook=_pairs,parse_constant=lambda x:(_ for _ in ()).throw(ValueError('NonFiniteArguments')))
        except (ValueError,RecursionError):raise ValueError('MalformedArguments') from None
    if type(arguments) is not dict or any(type(key) is not str for key in arguments):raise ValueError('InvalidArgumentsType')
    try:json.dumps(arguments,allow_nan=False)
    except (TypeError,ValueError,RecursionError):raise ValueError('InvalidArgumentsType') from None
    fn=tools[name];signature=inspect.signature(fn)
    try:bound=signature.bind(**arguments)
    except TypeError:raise ValueError('InvalidSignature') from None
    annotations=typing.get_type_hints(fn)
    if any(not _matches(value,annotations.get(key,signature.parameters[key].annotation)) for key,value in bound.arguments.items()):
        raise ValueError('InvalidArgumentType')
    return name,deepcopy(arguments),fn


class ToolRegistry:
    def __init__(self,ctx,*,arm,assets,runtime_identity):
        started=perf_counter()
        required={'graph_path','graph_sha256','embedding_path','embedding_sha256','source_manifest','workspace_root'}
        if arm not in ('B','C','C0') or type(assets) is not dict or set(assets)!=required:raise ValueError('InvalidRegistryAssets')
        if (ctx.budget.tool_calls!=8 or ctx.harness.command_timeout_seconds is None
            or not 0<ctx.harness.command_timeout_seconds<=30):raise ValueError('RegistryRuntimeBudgetMismatch')
        self.ctx=ctx;self.arm=arm;self.assets=deepcopy(assets)
        self.runtime_identity=validate_runtime(ctx,assets['workspace_root'],runtime_identity)
        self.source_manifest=validate_binding(assets['source_manifest'])
        if not self.source_manifest['files']:raise ValueError('MissingPinnedSources')
        if self.source_manifest['graph_sha256']!=assets['graph_sha256']:raise ValueError('RegistryGraphMismatch')
        if str(ctx.graph_dir)!=assets['graph_path'] or str(ctx.embeddings_dir)!=assets['embedding_path']:raise ValueError('OriginalFactoryAssetMismatch')
        checked_bytes=0
        for key in ('graph','embedding'):
            _,size=_read_checked(assets[key+'_path'],root=Path(assets[key+'_path']).parent,sha256=assets[key+'_sha256']);checked_bytes+=size
        self.reader=RuntimeSnapshotReader(ctx,workspace_root=assets['workspace_root'],runtime_identity=self.runtime_identity)
        self.native_session=None
        factories=[]
        if arm=='B':
            factories=[make_get_code_neighbors,make_search_similar_code,make_get_code_subgraph]
            functions=[factory(ctx) for factory in factories]
        else:
            self.native_session=NativeGraphWitnessSession(ctx,graph_path=assets['graph_path'],expected_graph_sha256=assets['graph_sha256'],
                source_manifest=self.source_manifest,workspace_root=assets['workspace_root'],runtime_identity=self.runtime_identity,
                topology_enabled=arm=='C',output_limit=OUTPUT_LIMIT)
            functions=[make_graph_find_paths(self.native_session),make_graph_compare_locations(self.native_session),make_search_similar_code(ctx)]
            factories=[make_graph_find_paths,make_graph_compare_locations,make_search_similar_code]
        factories += [make_read_file,make_run_command];functions += [make_read_file(ctx),make_run_command(ctx)]
        self._tools={fn.__name__:fn for fn in functions}
        self.factory_identities={fn.__name__:factory.__module__+'.'+factory.__name__ for fn,factory in zip(functions,factories)}
        self._schemas=[_schema(fn) for fn in functions]
        self._counters=dict(emitted=0,attempted=0,metered=0,suppressed=0)
        self._observations=[];self._audit=[];self._native_audit=[];self._source_checks=[];self._source_baseline={};self.closed=False;self.terminal_reason=None
        self._setup=dict(elapsed_seconds=perf_counter()-started,asset_bytes_hashed=checked_bytes,
            native_graph_setup=deepcopy(self.native_session.setup) if self.native_session else None,
            original_graph_setup='lazy_factory_work_unknown_until_call' if arm=='B' else 'seed_factory_lazy_work_unknown',
            native_tool_calls=0,wrapper_policy=WRAPPER_POLICY)
        # Establish validated sizes using the same paid runtime helper and pinned
        # hashes. An unsupported initial profile cannot invent a growth baseline.
        self.check_source_integrity(phase='initial_capture')
        self._setup['elapsed_seconds']=perf_counter()-started

    @property
    def counters(self):return deepcopy(self._counters)
    @property
    def setup(self):
        result=deepcopy(self._setup)
        if self.native_session is not None:result['native_graph_setup']=deepcopy(self.native_session.setup)
        return result
    @property
    def observations(self):return deepcopy(self._observations)
    @property
    def audit(self):return deepcopy(self._audit)
    @property
    def native_audit(self):return deepcopy(self._native_audit)
    @property
    def source_checks(self):return deepcopy(self._source_checks)

    def schemas(self):return deepcopy(self._schemas)

    def check_source_integrity(self, *, phase):
        """Paid internal runtime byte scans; no host fallback, repair or slots."""
        started=perf_counter();work=dict(runtime_exec_operations=0,opened_files=0,bytes_read=0,bytes_hashed=0,bytes_transferred=0,incomplete=False)
        records={};transports=[];status='ok'
        paths=list(self.source_manifest['files'])
        for offset in range(0,len(paths),8):
            batch=paths[offset:offset+8]
            capture=self.reader.read_files(batch,limits={})
            transports.append(deepcopy(capture.get('transport')))
            for key in work:
                if key=='incomplete':work[key]=work[key] or capture['work'].get(key,True)
                elif work[key] is None or capture['work'].get(key) is None:work[key]=None
                else:work[key]+=capture['work'][key]
            for path,record in capture['records'].items():
                record_status=record['status']
                if record_status=='ok' and record['sha256']!=self.source_manifest['files'][path]:record_status='SourceHashMismatch'
                baseline=self._source_baseline.get(path)
                stat=record.get('stat_before')
                observed=stat.get('size') if type(stat) is dict else None
                known_growth=(record_status=='SourceTooLarge' and baseline is not None
                    and type(stat) is dict and set(stat)=={'dev','ino','size','mtime_ns','ctime_ns'}
                    and all(type(value) is int for value in stat.values())
                    and observed>HARD_LIMITS['max_file_bytes'] and observed>baseline['size'])
                classification='known_pinned_source_growth' if known_growth else None
                records[path]=dict(status=record_status,sha256=record.get('sha256'),size=record.get('size'),
                    baseline_size=baseline['size'] if baseline else None,observed_size=observed,
                    classification=classification)
                if record_status=='ok' and phase=='initial_capture':
                    self._source_baseline[path]=dict(size=record['size'],sha256=record['sha256'])
                if record_status!='ok':
                    if known_growth or record_status in ('SourceHashMismatch','UnsafePath','HardlinkRejected','NotRegularFile','SourceRace'):status='source_mutation'
                    elif status!='source_mutation':status='source_integrity_error'
            if status!='ok':break
        result=dict(phase=phase,status=status,work=work,records=records,transport=transports,elapsed_seconds=perf_counter()-started,
            model_tool_slots=0,capture_limit_scope='fixed_helper_stdout_emission_and_post_return_acceptance_only',
            upstream_capture_limit_bytes=None,upstream_clipped=None)
        self._source_checks.append(deepcopy(result))
        if status!='ok':self.terminal_reason=status;self.close()
        return result

    def _gate_invalid(self):
        # Exactly the same native context budget gate, with no tool work.
        try:return self.ctx.check_budget(count_tool_call=True)
        except Exception as exc:
            self.terminal_reason='dispatch_error';self.close()
            return json.dumps(dict(status='error',error_type='NativeBudgetGateError',exception_type=type(exc).__name__,
                                   accounting=dict(tool_internal_work=None)))

    def dispatch(self,call,*,round_index):
        with self.ctx.tool_lock:
            started=perf_counter();self._counters['emitted']+=1
            before=getattr(self.ctx,'tool_calls_used',None);raw=None;tool_raw=None
            # Admission precedes preservation, parsing and every validation step.
            admitted=not self.closed and self._counters['attempted']<8
            if admitted:self._counters['attempted']+=1
            else:self._counters['suppressed']+=1
            original=_snapshot(call)
            call_id=_snapshot(call.get('id')) if type(call) is dict else None
            function=call.get('function') if type(call) is dict else None
            name=_snapshot(function.get('name')) if type(function) is dict else None
            arguments=_snapshot(function.get('arguments')) if type(function) is dict else None
            status='fulfilled';error=None;executed=False;gate_calls=0
            native_before=len(self._native_audit)
            if self.closed:
                status='session_closed';raw=dict(status='error',error_type='SessionClosed')
            elif not admitted:
                status='rejected_budget';raw=dict(status='error',error_type='BudgetExceeded')
            else:
                try:name,arguments,fn=_parse_call(call,self._tools)
                except ValueError as exc:
                    error=str(exc);gate_calls=1;gate=self._gate_invalid();status='invalid_call'
                    raw=parse_tool_response(gate) if gate else dict(status='error',error_type=error)
                    if self.terminal_reason=='dispatch_error':status='dispatch_error'
                else:
                    integrity=self.check_source_integrity(phase='before_tool')
                    if integrity['status']!='ok':
                        gate_calls=1;gate=self._gate_invalid();status=integrity['status'];raw=dict(status='error',error_type=status)
                        if self.terminal_reason=='dispatch_error':status='dispatch_error';raw=parse_tool_response(gate)
                    else:
                        try:
                            gate_calls=1;executed=True;raw=fn(**arguments);tool_raw=_snapshot(raw)
                        except Exception as exc:
                            error=type(exc).__name__;status='tool_error';raw=dict(status='error',error_type='ToolExecutionError',exception_type=error,accounting=dict(tool_internal_work=None))
                            self.terminal_reason='tool_error';self.close()
                        finally:
                            if self.native_session is not None:
                                # Capture even when native packaging raises after
                                # its gate/work ledger has already been updated.
                                self._native_audit=self.native_session.audit_ledger
                            integrity=self.check_source_integrity(phase='after_tool')
                        if integrity['status']!='ok':status=integrity['status'];raw=dict(status='error',error_type=status)
            after=getattr(self.ctx,'tool_calls_used',None)
            delta=after-before if type(before) is int and type(after) is int and after>=before else None
            if delta is None:self._counters['metered']=None
            elif self._counters['metered'] is not None:self._counters['metered']+=delta
            delivered=bound_result(raw);text=json.dumps(delivered)
            observation=dict(tool_call_id=call_id,tool_name=name,arguments=deepcopy(arguments),delivered_text=text,runtime_identity=deepcopy(self.runtime_identity))
            self._observations.append(deepcopy(observation))
            record=dict(status=status,tool_call_id=call_id,tool_name=name,arguments=deepcopy(arguments),round_index=round_index,
                delivered_text=text,native_calls_before=before,native_calls_after=after,native_call_delta=delta,native_gate_calls=gate_calls,
                tool_callable_invoked=executed,elapsed_seconds=perf_counter()-started,error=error,
                tool_internal_work='native_accounting_in_result' if name in ('graph_find_paths','graph_compare_locations') and executed else None,
                output_characters=len(text),runtime_identity=deepcopy(self.runtime_identity))
            self._audit.append(dict(original_call=original,raw_result=_snapshot(tool_raw if tool_raw is not None else raw),dispatch=deepcopy(record),
                native_preprojection_audit=deepcopy(self._native_audit[native_before:]),native_preprojection_citation_evidence=False,
                raw_result_citation_evidence=False,upstream_capture_limit_bytes=None,upstream_clipped=None))
            return deepcopy(record)

    def close(self):
        with self.ctx.tool_lock:
            if self.closed:return
            self.closed=True
            if self.native_session is not None:self.native_session.close()


def make_registry(ctx, *, arm, assets, runtime_identity):
    return ToolRegistry(ctx,arm=arm,assets=assets,runtime_identity=runtime_identity)
