"""Offline native retrieval adapters and shared metering; no model transport.

The old SDK retrieval functions execute unchanged with a preloaded graph. We
intentionally bypass embedding initialization and the ADK/Docker wrapper. This
is retrieval-core parity, not end-to-end original runtime parity.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from time import perf_counter
from types import ModuleType

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


VARIANT = load('_allpaths_unchanged_variant',ROOT/'variants/graph-find-paths-all-v1/tool.py')


def sdk():
    name='swegemma.graph.graph_utils'
    if name in sys.modules:
        module=sys.modules[name]
        assert Path(module.__file__).resolve()==ROOT/'vendor/swegemma/graph/graph_utils.py'
        return module
    if any(n=='swegemma' or n.startswith('swegemma.') for n in sys.modules):
        raise RuntimeError('Use a fresh isolated process for the native adapter')
    # Namespace containers avoid SDK __init__ importing unrelated model runtimes.
    # The two actual modules are loaded verbatim; no algorithms are replaced.
    for name,path in [('swegemma',ROOT/'vendor/swegemma'),('swegemma.graph',ROOT/'vendor/swegemma/graph')]:
        package=ModuleType(name)
        package.__path__=[str(path)]
        sys.modules[name]=package
    embedding=load('swegemma.graph.embedding_utils',ROOT/'vendor/swegemma/graph/embedding_utils.py')
    sys.modules['swegemma.graph'].embedding_utils=embedding
    return load('swegemma.graph.graph_utils',ROOT/'vendor/swegemma/graph/graph_utils.py')


class NativeBaseline:
    def __init__(self,graph_path,expected_sha256):
        # Same file/SHA/node/row validation as candidate, separately timed by caller.
        validated=VARIANT._GraphWitnessSession(graph_path,expected_sha256)
        self.graph_sha256=validated.graph_sha256
        self.sdk=sdk()
        self.graph=self.sdk.load_graph_from_json(str(graph_path))
        self.repo=Path(graph_path).stem
        self.graph.graph['repo_name']=self.repo

    def query(self,name,args):
        if name=='get_code_neighbors':
            values=self.sdk.get_neighbor(node=args['node'],graph=self.graph,
                max_neighbors=args.get('max_neighbors',20000),
                edge_type=args.get('edge_type'),repo_name=self.repo)
            return dict(status='ok',node=args['node'],neighbors=values,count=len(values),is_truncated=False)
        if name=='get_code_subgraph':
            sub=self.sdk.get_induced_subgraph(self.graph,args['nodes'])
            edges=[{'from':str(a),'to':str(b),'type':d.get('type') or d.get('relation','connected')}
                   for a,b,d in sub.edges(data=True)]
            return dict(status='ok',nodes=list(sub.nodes),edges=edges,node_count=len(sub),
                        edge_count=len(edges),is_truncated=False)
        raise ValueError('Unknown baseline tool: '+name)


class NativeCandidate:
    def __init__(self,graph_path,expected_sha256,config):
        limits=config['candidate']
        self.session=VARIANT.AllPathsSession(graph_path,expected_sha256,
            max_edge_rows=limits['max_edge_rows'],max_dfs_states=limits['max_dfs_states'],
            max_paths=limits['max_paths'],output_limit=config['max_result_chars'])

    def query(self,name,args):
        if name!='graph_find_paths': raise ValueError('Unknown candidate tool: '+name)
        return self.session.graph_find_paths(**args)


class BudgetExceeded(RuntimeError):
    pass


class MeteredInvoker:
    def __init__(self,backend,config):
        self.backend=backend
        self.config=config
        self.graph_attempts=0
        self.native_calls=0
        self.submit_attempts=0
        self.submitted=None
        self.traces=[]
        self.delivered_characters=0
        self.started=perf_counter()

    def _reject(self,name,args,reason):
        self.traces.append(dict(name=name,args=deepcopy(args),raw=None,delivered=None,
            error=reason,graph_attempts=self.graph_attempts,native_calls=self.native_calls,
            admitted=False))
        raise BudgetExceeded(reason)

    def __call__(self,name,args):
        started=perf_counter()
        if name=='submit_graph_answer':
            self.submit_attempts+=1
            if self.submit_attempts>self.config['max_submissions']:
                self._reject(name,args,'submission_limit')
            if perf_counter()-self.started>=self.config['max_episode_seconds']:
                self._reject(name,args,'episode_time_limit')
            valid=(isinstance(args,dict) and set(args)=={'paths','complete'}
                   and isinstance(args['paths'],list) and type(args['complete']) is bool)
            result=dict(status='ok' if valid else 'error',format_valid=valid)
        else:
            self.graph_attempts+=1
            if self.graph_attempts>self.config['max_graph_queries']:
                self._reject(name,args,'query_limit')
            if perf_counter()-self.started>=self.config['max_episode_seconds']:
                self._reject(name,args,'episode_time_limit')
            self.native_calls+=1
            try:
                result=self.backend.query(name,args)
            except Exception as error:
                result=dict(status='error',error_type=type(error).__name__,error_message=str(error))
        raw=deepcopy(result)
        raw_chars=len(json.dumps(raw))
        failure=None
        if perf_counter()-self.started>=self.config['max_episode_seconds']:
            failure='episode_time_limit'
        elif raw_chars>self.config['max_result_chars']:
            failure='output_limit'
        elif self.delivered_characters+raw_chars>self.config['max_total_result_chars']:
            failure='total_output_limit'
        if failure:
            result=dict(status='error',error_type=failure)
        text=json.dumps(result)
        if perf_counter()-self.started>=self.config['max_episode_seconds']:
            result=dict(status='error',error_type='episode_time_limit')
            text=json.dumps(result)
        if self.delivered_characters+len(text)>self.config['max_total_result_chars']:
            self.traces.append(dict(name=name,args=deepcopy(args),raw=raw,delivered=None,
                error='total_output_limit_no_room_for_error',graph_attempts=self.graph_attempts))
            raise BudgetExceeded('total_output_limit')
        assert len(text)<=self.config['max_result_chars']
        self.delivered_characters+=len(text)
        if name=='submit_graph_answer' and result.get('status')=='ok':
            self.submitted=deepcopy(args)
        self.traces.append(dict(name=name,args=deepcopy(args),raw=raw,delivered=deepcopy(result),
            delivered_text=text,raw_characters=raw_chars,delivered_characters=len(text),
            query_seconds=perf_counter()-started,graph_attempts=self.graph_attempts,
            native_calls=self.native_calls,admitted=True))
        return deepcopy(result)
