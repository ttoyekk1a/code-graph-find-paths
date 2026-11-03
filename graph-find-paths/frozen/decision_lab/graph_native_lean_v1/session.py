"""A graph-only, context-bound projection of the unchanged witness core.

The protected core ``_setup`` snapshot is an explicit compatibility dependency.
This module never asks for core.nodes, reads source, or repairs graph topology.
"""
from copy import deepcopy
import json
from time import perf_counter

from swegemma.tools.base import budget_gated, parse_tool_response
from decision_lab.graph_native_v1.core import GraphWitnessSession
from decision_lab.graph_native_v1.source_read import live_identity, validate_runtime


_GRAPH_COUNTERS = ('visited_nodes', 'edge_rows_examined', 'canonical_relations_examined',
                   'elapsed_seconds')
_METHODS = ('graph_find_paths', 'graph_compare_locations')
_NEXT_ACTION = {
    'complete': 'Use the returned paths and bounded absence cells as graph evidence; submit only claims they support.',
    'partial': 'Use only retained complete evidence; leave unknown pairs unresolved or submit unavailable if the task requires them.',
    'topology_disabled': 'No edge evidence is available in this mode; submit unavailable for tasks requiring stored graph connections.',
    'unresolved': 'Use exact graph node IDs or an unambiguous suffix; unresolved pairs provide no graph claim.',
    'unavailable': 'No usable graph evidence was delivered; submit unavailable if the required claims remain unsupported.',
    'InvalidArguments': 'Supply bounded lists of nonempty node IDs and an integer max_hops from 1 to 4; booleans are invalid.',
    'SessionClosed': 'Stop using this closed graph session and finalize with the evidence already delivered.',
    'RuntimeChanged': 'Stop graph queries in this session; the runtime identity no longer matches its approved context.',
    'BudgetExceeded': 'Do not retry this graph call; finalize using existing evidence within any remaining submission allowance.',
    'OutputLimitExceeded': 'The required evidence packet cannot fit the output cap; do not treat omitted pairs as evidence or repeat this unchanged request.',
    'CoreExecutionError': 'Graph execution failed; do not infer missing connections from this response.',
    'PackagingError': 'Evidence packaging failed; do not infer graph claims from the omitted result.',
}


def _graph_work(unknown=False):
    return dict(visited_nodes=None if unknown else 0,
                edge_rows_examined=None if unknown else 0,
                canonical_relations_examined=None if unknown else 0,
                elapsed_seconds=None if unknown else 0.0, incomplete=unknown)


def _measure(packet):
    """Default JSON character/UTF-8 measurements, including their own fields."""
    account = packet['accounting']
    for _ in range(32):
        raw = json.dumps(packet)
        chars, size = len(raw), len(raw.encode('utf-8'))
        if (account['output_characters'], account['output_bytes']) == (chars, size):
            return chars
        account.update(output_characters=chars, output_bytes=size)
    raise RuntimeError('OutputAccountingDidNotConverge')


class LeanGraphWitnessSession:
    """One cold graph load, one original gate and at most one core query per call.

    A delivered positive remains usable during an incomplete search. A negative
    means only no stored undirected adjacency path within the requested hop
    bound in this exact graph, for an anchor whose bounded traversal completed.
    Identity, unresolved endpoints and omitted witnesses are unknown. Source
    grounding is never performed. Audit is engineering-only, not tool evidence.
    """
    def __init__(self, ctx, *, graph_path, expected_graph_sha256,
                 workspace_root='/workspace', runtime_identity, topology_enabled=True,
                 output_limit=8000, core_output_limit=6000):
        started = perf_counter()
        if type(output_limit) is not int or not 2048 <= output_limit <= 8000:
            raise ValueError('InvalidOutputLimit')
        if type(core_output_limit) is not int or not 512 <= core_output_limit <= 6000:
            raise ValueError('InvalidCoreOutputLimit')
        self.ctx = ctx
        self.workspace_root = workspace_root
        self.runtime_identity = validate_runtime(ctx, workspace_root, runtime_identity)
        self._initial_identity = deepcopy(self.runtime_identity['identity'])
        self.core = GraphWitnessSession(graph_path, expected_graph_sha256,
                                       topology_enabled=topology_enabled, output_limit=core_output_limit)
        self.graph_sha256 = self.core.graph_sha256
        self.topology_enabled = topology_enabled
        self.output_limit = output_limit
        self.core_output_limit = core_output_limit
        self.closed = False
        self.poisoned = False
        self._native = dict(attempts=0, admitted=0, rejected=0)
        self._cumulative = _graph_work()
        self._cumulative['total_elapsed_seconds'] = 0.0
        self._audit_ledger = []
        self._setup = dict(elapsed_seconds=0.0,
                           core_setup=deepcopy(self.core._setup),
                           output_limit=output_limit, core_output_limit=core_output_limit,
                           source_reads=0, source_parses=0, nodes_copy_count=0,
                           nodes_copied=0, native_tool_calls=0)
        self._setup['elapsed_seconds'] = perf_counter()-started

    @property
    def setup(self):
        with self.ctx.tool_lock:
            return deepcopy(self._setup)

    @property
    def native(self):
        with self.ctx.tool_lock:
            return deepcopy(self._native)

    @property
    def cumulative(self):
        with self.ctx.tool_lock:
            return deepcopy(self._cumulative)

    @property
    def audit_ledger(self):
        with self.ctx.tool_lock:
            return deepcopy(self._audit_ledger)

    def close(self):
        with self.ctx.tool_lock:
            self.closed = True

    def _identity_matches(self):
        try:
            matches = live_identity(self.ctx, self.workspace_root) == self._initial_identity
        except Exception:
            matches = False
        if not matches:
            self.poisoned = True
        return not self.poisoned

    def _packet(self, hops):
        return dict(version='lean-graph-v1', status='unavailable', reason='no_graph_evidence',
                    graph_sha256=self.graph_sha256, mode='C' if self.topology_enabled else 'C0',
                    semantics='stored_undirected_adjacency', source_grounding='not_performed',
                    requested_bound=dict(max_hops=hops if type(hops) is int and 1 <= hops <= 4 else None,
                                         max_visited_nodes=160, max_edge_rows=800, budget_scope='whole_query'),
                    resolution=dict(sources=[], targets=[]), paths=[], matrix=[], frontier=None,
                    omissions=dict(paths=0, resolution=0, matrix=0, whole_payload=False),
                    accounting=dict(graph=_graph_work(), core_calls=0, source_reads=0, source_parses=0,
                                    setup_detail='audit_only', common_guard_work='registry_source_checks_separate',
                                    output_characters=0, output_bytes=0),
                    next_action=_NEXT_ACTION['unavailable'])

    @staticmethod
    def _error(packet, error_type):
        packet.update(status='error', reason=error_type, error_type=error_type,
                      paths=[], matrix=[], resolution=dict(sources=[], targets=[]),
                      next_action=_NEXT_ACTION[error_type])
        return packet

    def _normalize(self, packet, core):
        packet['resolution'] = deepcopy(core['resolution'])
        packet['paths'] = deepcopy(core['paths'])
        packet['frontier'] = deepcopy(core['frontier'])
        packet['omissions']['paths'] = core['omissions']['paths']
        paths = {path['path_id'] for path in packet['paths']}
        complete = {entry['anchor_id']: entry['complete'] for entry in core['frontier']['by_anchor']}
        sources, targets = core['resolution']['sources'], core['resolution']['targets']
        for original in core['matrix']:
            cell = dict(anchor_index=original['anchor_index'], candidate_index=original['candidate_index'],
                        state='unknown', path_ids=[])
            state = original['state']
            anchor = GraphWitnessSession._resolved_id(sources[cell['anchor_index']])
            target = GraphWitnessSession._resolved_id(targets[cell['candidate_index']])
            if not self.topology_enabled:
                cell['reason'] = 'topology_disabled'
            elif state == 'witnessed' and original['path_ids'] and all(p in paths for p in original['path_ids']):
                cell.update(state='connected', path_ids=deepcopy(original['path_ids']))
            elif state == 'hop_bound' and anchor is not None and target is not None and anchor != target and complete.get(anchor):
                cell['state'] = 'no_path_within_bound'
            else:
                cell['reason'] = {'identity': 'identity_only_no_edge_support',
                                  'topology_disabled': 'topology_disabled',
                                  'budget_exhausted': 'query_budget_exhausted',
                                  'witnessed': 'path_omitted',
                                  'witnessed_omitted': 'path_omitted'}.get(state, 'unresolved_or_ambiguous_endpoint')
            packet['matrix'].append(cell)
        self._classify(packet)
        return packet

    def _classify(self, packet):
        if packet['status'] == 'error':
            return
        matrix = packet['matrix']
        usable = sum(c['state'] != 'unknown' for c in matrix)
        if not self.topology_enabled:
            packet.update(status='unavailable', reason='topology_disabled')
        elif (matrix and usable == len(matrix) and packet['frontier']['complete']
              and not packet['omissions']['paths']):
            packet.update(status='complete', reason='bounded_evidence_complete')
        elif usable:
            packet.update(status='partial', reason='bounded_evidence_incomplete')
        else:
            packet.update(status='unavailable', reason='no_graph_evidence')
        if not self.topology_enabled:
            action = 'topology_disabled'
        elif packet['status'] in ('complete', 'partial'):
            action = packet['status']
        elif any(c.get('reason') == 'unresolved_or_ambiguous_endpoint' for c in matrix):
            action = 'unresolved'
        else:
            action = 'unavailable'
        packet['next_action'] = _NEXT_ACTION[action]

    def _run_core(self, packet, method, left, right, hops, record):
        if self.closed:
            return self._error(packet, 'SessionClosed')
        if not self._identity_matches():
            return self._error(packet, 'RuntimeChanged')
        try:
            if method not in _METHODS:
                raise ValueError('InvalidMethod')
            GraphWitnessSession._validate_query(left, right, hops)
        except (TypeError, ValueError):
            return self._error(packet, 'InvalidArguments')
        record['stage'] = 'core_started'
        packet['accounting']['core_calls'] = 1
        querying = perf_counter()
        try:
            core = getattr(self.core, method)(left, right, hops)
        except Exception:
            record['core_call_seconds'] = perf_counter()-querying
            packet['accounting']['graph'] = _graph_work(unknown=True)
            # Identity changes poison the session even when the core failed.
            error = 'CoreExecutionError' if self._identity_matches() else 'RuntimeChanged'
            return self._error(packet, error)
        record['core_call_seconds'] = perf_counter()-querying
        packet['accounting']['graph'] = deepcopy(core['work']) | {'incomplete': False}
        record['stage'] = 'core_returned'
        copying = perf_counter()
        record['core_result'] = deepcopy(core)
        record['core_result_copy_seconds'] = perf_counter()-copying
        serializing = perf_counter()
        raw = json.dumps(core)
        record['core_output_characters'] = len(raw)
        record['core_output_bytes'] = len(raw.encode('utf-8'))
        record['core_measurement_seconds'] = perf_counter()-serializing
        record['core_result_already_bounded'] = True
        if not self._identity_matches():
            omitted = core.get('omissions', {}).get('paths')
            total_omitted = None if omitted is None else omitted + len(core.get('paths', []))
            packet['omissions'].update(paths=total_omitted, resolution=len(left)+len(right),
                                       matrix=len(left)*len(right), whole_payload=True)
            return self._error(packet, 'RuntimeChanged')
        if 'error' in core:
            packet['frontier'] = dict(complete=core.get('frontier_complete'), pending_nodes=core.get('pending_nodes'))
            packet['omissions'].update(paths=None, resolution=len(left)+len(right),
                                       matrix=len(left)*len(right), whole_payload=True)
            return self._error(packet, 'OutputLimitExceeded' if core['error'] == 'OutputLimitExceeded' else 'CoreExecutionError')
        normalizing = perf_counter()
        packet = self._normalize(packet, core)
        record['normalization_seconds'] = perf_counter()-normalizing
        return packet

    def _bound(self, packet):
        while _measure(packet) > self.output_limit and packet['paths']:
            removed = packet['paths'].pop()
            packet['omissions']['paths'] += 1
            for cell in packet['matrix']:
                if removed['path_id'] in cell['path_ids']:
                    cell.update(state='unknown', path_ids=[], reason='path_omitted')
            self._classify(packet)
        if _measure(packet) <= self.output_limit:
            return packet
        omissions = packet['omissions']
        omissions['resolution'] += sum(len(v) for v in packet['resolution'].values())
        omissions['matrix'] += len(packet['matrix'])
        omissions['whole_payload'] = True
        frontier = packet['frontier']
        self._error(packet, 'OutputLimitExceeded')
        packet['frontier'] = (dict(complete=frontier.get('complete'), pending_nodes=frontier.get('pending_nodes'))
                              if frontier is not None else None)
        if _measure(packet) > self.output_limit:
            # All mandatory fields now have bounded scalar sizes. Constructor
            # minimum 2048 leaves room for truthful known/null work and gate.
            raise RuntimeError('MandatoryAccountingEnvelopeTooLarge')
        return packet

    def _invoke(self, method, left, right, hops):
        with self.ctx.tool_lock:
            started = perf_counter()
            record = dict(method=method, arguments=deepcopy(dict(left=left, right=right, hops=hops)),
                          stage='gate', core_result=None, core_result_already_bounded=True,
                          core_call_seconds=0.0, core_result_copy_seconds=0.0,
                          core_measurement_seconds=0.0, normalization_seconds=0.0,
                          native_serialization_seconds=0.0)
            self._native['attempts'] += 1
            admitted = False
            packet = self._packet(hops)

            @budget_gated
            def execute(ctx):
                nonlocal admitted, packet
                admitted = True
                self._native['admitted'] += 1
                try:
                    packet = self._run_core(packet, method, left, right, hops, record)
                except Exception:
                    if record['stage'] == 'core_started':
                        packet['accounting']['graph'] = _graph_work(unknown=True)
                    self._error(packet, 'CoreExecutionError' if record['stage'] == 'core_started' else 'PackagingError')
                serializing = perf_counter()
                raw = json.dumps(packet)
                record['native_serialization_seconds'] = perf_counter()-serializing
                return raw

            try:
                gated = parse_tool_response(execute(self.ctx))
                if admitted:
                    packet = gated
                else:
                    self._native['rejected'] += 1
                    self._error(packet, 'BudgetExceeded')
            except Exception:
                if not admitted:
                    self._native['rejected'] += 1
                elif record['stage'] == 'core_started':
                    packet['accounting']['graph'] = _graph_work(unknown=True)
                self._error(packet, 'PackagingError')
            account = packet['accounting']
            account.update(native_budget_decision='admitted' if admitted else 'rejected',
                           native=deepcopy(self._native), elapsed_seconds=perf_counter()-started,
                           elapsed_scope='before_final_output_serialization')
            graph = account['graph']
            self._cumulative['incomplete'] |= graph['incomplete']
            for key in _GRAPH_COUNTERS:
                current, value = self._cumulative[key], graph[key]
                self._cumulative[key] = None if current is None or value is None else current + value
            self._cumulative['total_elapsed_seconds'] += account['elapsed_seconds']
            copying = perf_counter()
            record.update(gate_decision=account['native_budget_decision'],
                          normalized_before_final_limiter=deepcopy(packet),
                          setup=deepcopy(self._setup), native=deepcopy(self._native),
                          cumulative=deepcopy(self._cumulative))
            record['pre_limiter_snapshot_seconds'] = perf_counter()-copying
            # Persist the pre-limiter receipt first; packaging cannot erase paid
            # work or the original core-bounded response from audit history.
            self._audit_ledger.append(record)
            finalizing = perf_counter()
            try:
                packet = self._bound(packet)
            except Exception:
                # No graph retry: discard the whole unusable projection while
                # preserving actual paid work and the pre-limiter audit receipt.
                omissions = packet['omissions']
                if omissions['paths'] is not None:
                    omissions['paths'] += len(packet['paths'])
                omissions['resolution'] += sum(len(v) for v in packet['resolution'].values())
                omissions['matrix'] += len(packet['matrix'])
                omissions['whole_payload'] = True
                self._error(packet, 'PackagingError')
                packet['frontier'] = None
                _measure(packet)
            record.update(stage='delivered', final_output_characters=account['output_characters'],
                          final_output_bytes=account['output_bytes'],
                          final_projection_seconds=perf_counter()-finalizing,
                          invocation_seconds_before_final_receipt_update=perf_counter()-started)
            return packet


def make_graph_find_paths(session):
    def graph_find_paths(source_nodes: list[str], target_nodes: list[str], max_hops: int = 4) -> dict:
        """Find one shortest stored undirected graph-only adjacency witness per pair.

        Exact IDs or unambiguous suffixes resolve endpoints. Whole-query bounds
        are 160 visited nodes and 800 original edge rows. Cells are connected,
        no_path_within_bound, or unknown; identity is unknown. Bounded absence
        requires resolved distinct endpoints and a completed anchor search; it
        says nothing about source/runtime behavior. source_grounding=not_performed.

        Args:
            source_nodes: One to three exact graph node IDs or unambiguous suffixes.
            target_nodes: One to eight exact graph node IDs or unambiguous suffixes.
            max_hops: Positive hop bound from one through four, excluding bool.

        Returns:
            Complete original-row paths, per-pair states, frontier and accounting.
            Status is complete, partial, unavailable, or error. No bug ranking.
        """
        return session._invoke('graph_find_paths', source_nodes, target_nodes, max_hops)
    return graph_find_paths


def make_graph_compare_locations(session):
    def graph_compare_locations(anchor_nodes: list[str], candidate_nodes: list[str], max_hops: int = 4) -> dict:
        """Compare graph-only stored undirected adjacency support, without ranking.

        Exact IDs or unambiguous suffixes resolve endpoints. One shortest full
        witness per pair shares whole-query bounds of 160 visited nodes and 800
        original edge rows. Cells are connected, no_path_within_bound, or unknown.
        Bounded absence requires a completed anchor search and resolved distinct
        endpoints. Identity/omissions/incomplete searches are unknown. Absence
        concerns only this graph and hop bound, not source/runtime behavior.
        source_grounding=not_performed.

        Args:
            anchor_nodes: One to three exact graph node IDs or unambiguous suffixes.
            candidate_nodes: One to eight exact graph node IDs or unambiguous suffixes.
            max_hops: Positive hop bound from one through four, excluding bool.

        Returns:
            Complete original-row paths, normalized support matrix and accounting.
            Status is complete, partial, unavailable, or error. No bug ranking.
        """
        return session._invoke('graph_compare_locations', anchor_nodes, candidate_nodes, max_hops)
    return graph_compare_locations
