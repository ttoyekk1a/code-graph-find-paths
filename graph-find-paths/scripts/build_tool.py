"""Portable assembly of the unchanged experimental D tool; no model or launch.

Production callers supply their existing real SWE-gemma context and ownership.
This file does not construct or attest a runtime and does not implement a solver.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'frozen'
TOOL_SCHEMA=json.loads((ROOT/'references/tool-schema.json').read_text())

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def verify_sources():
    for row in json.loads((ROOT/'references/source-provenance.json').read_text()):
        if digest(ROOT/row['path']) != row['sha256']:
            raise RuntimeError('FrozenSourceMismatch:'+row['path'])

verify_sources()
# Avoid silently using a different copy already imported by another experiment.
for name, mod in list(sys.modules.items()):
    if name.startswith(('decision_lab.graph_native_v1','decision_lab.graph_native_lean_v1')):
        loc=getattr(mod,'__file__',None)
        if loc and not Path(loc).resolve().is_relative_to(FROZEN):
            raise RuntimeError('ConflictingModule: use an isolated worker process: '+name)
sys.path.insert(0,str(FROZEN))
SEARCH_FILE=FROZEN/'evidence/path-study-expanded-20261103/search.py'
_spec=importlib.util.spec_from_file_location('_repro_frozen_d_search',SEARCH_FILE)
_search=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_search)

def build_graph_find_paths(ctx, *, graph_path, expected_graph_sha256, runtime_identity,
                           workspace_root='/workspace'):
    """Return (exact-named tool function, session); caller closes session finally."""
    # The original SDK is supplied by the target harness, not replaced by this package.
    import swegemma.tools.base as base
    if digest(base.__file__) != digest(FROZEN/'vendor/swegemma/tools/base.py'):
        raise RuntimeError('BudgetGateVersionMismatch: audit harness version before integration')
    from decision_lab.graph_native_lean_v1.session import LeanGraphWitnessSession,make_graph_find_paths
    session=LeanGraphWitnessSession(ctx,graph_path=graph_path,
        expected_graph_sha256=expected_graph_sha256,runtime_identity=runtime_identity,
        workspace_root=workspace_root,topology_enabled=True,output_limit=8000,core_output_limit=6000)
    session.core=_search.TargetedCore.from_loaded(session.core,
        mode='bidirectional',max_nodes=160,max_rows=800)
    tool=make_graph_find_paths(session)
    # Use the observed model schema, not the legacy shortest-path docstring.
    tool.__doc__=TOOL_SCHEMA['function']['description']
    assert tool.__name__==TOOL_SCHEMA['function']['name']=='graph_find_paths'
    return tool,session
