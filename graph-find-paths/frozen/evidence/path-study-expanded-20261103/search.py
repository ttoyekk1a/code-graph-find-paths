"""Experiment-only target-aware search over the unchanged validated graph index.

No graph read, global mutation, source inspection or retry occurs here. Index
objects are shared read-only by convention; all traversal state is query-local.
Positive paths are legal bounded witnesses, without a shortest-path guarantee.
"""
from copy import deepcopy
from functools import partial

from decision_lab.graph_native_v1.core import GraphWitnessSession


_INDEX_FIELDS = ('topology_enabled', 'output_limit', 'graph_path', 'graph_sha256',
                 '_nodes', '_ids', '_relations', '_adjacency', '_setup')
COMPACT_EVIDENCE_KEYS = ('graph_sha256', 'semantics', 'source_grounding', 'resolution', 'paths',
                         'matrix', 'frontier', 'status', 'reason', 'error_type',
                         'omissions', 'requested_bound', 'next_action')


def _clone(core, cls, max_nodes, max_rows):
    if not isinstance(core, GraphWitnessSession):
        raise ValueError('InvalidLoadedCore')
    if any(type(value) is not int or value < 1 for value in (max_nodes, max_rows)):
        raise ValueError('InvalidSearchBudget: positive integers required')
    result = cls.__new__(cls)
    for key in _INDEX_FIELDS:
        setattr(result, key, getattr(core, key))
    result.MAX_VISITED_NODES = max_nodes
    result.MAX_EDGE_ROWS = max_rows
    return result


def clone_radius(core, max_nodes=160, max_rows=800):
    """Fresh frozen-core instance; share index, override budgets on instance only."""
    return _clone(core, GraphWitnessSession, max_nodes, max_rows)


def compact_packet(packet):
    """Lossless projection of proof fields AFTER the native output limiter.

    Subsetting and deep copying cannot enlarge standard JSON serialization.
    Accounting is retained in the native audit, never passed off as new evidence.
    """
    return {key: deepcopy(packet[key]) for key in COMPACT_EVIDENCE_KEYS if key in packet}


class TargetedCore(GraphWitnessSession):
    @classmethod
    def from_loaded(cls, core, mode='bidirectional', max_nodes=160, max_rows=800):
        if mode not in ('earlystop', 'bidirectional'):
            raise ValueError('InvalidSearchMode')
        result = _clone(core, cls, max_nodes, max_rows)
        result.mode = mode
        result.last_search_audit = {}
        return result

    def _query(self, sources, targets, hops, compare):
        self._validate_query(sources, targets, hops)
        source = self._resolved_id(self._resolve(sources[0])) if len(sources) == 1 else None
        target = self._resolved_id(self._resolve(targets[0])) if len(targets) == 1 else None
        if (compare or not self.topology_enabled or source is None or target is None
                or source == target):
            self.last_search_audit = {'algorithm': 'radius_fallback'}
            return clone_radius(self, self.MAX_VISITED_NODES, self.MAX_EDGE_ROWS)._query(
                sources, targets, hops, compare)

        # An invocation-local proxy gives frozen _query its expected traversal
        # interface. No request target is stored on the shared caller or class.
        proxy = clone_radius(self, self.MAX_VISITED_NODES, self.MAX_EDGE_ROWS)
        audit = {'algorithm': self.mode}
        proxy._traverse = partial(self._target_traverse, target=target, audit=audit)
        proxy._bound = partial(self._target_bound, proxy=proxy)
        self.last_search_audit = audit
        return proxy._query(sources, targets, hops, compare=False)

    @staticmethod
    def _target_bound(result, started, *, proxy):
        # The frozen extractor's final limitation describes its original BFS.
        # Replace that claim before measuring/limiting this new algorithm's
        # packet; no previously bounded output is enlarged afterward.
        result['limitations'][-1] = 'One legal bounded witness per pair; source grounding not performed.'
        return GraphWitnessSession._bound(proxy, result, started)

    @staticmethod
    def _joined_path(source, target, meeting, parents):
        left_nodes, left_relations = [meeting], []
        node = meeting
        while node != source:
            node, relation = parents[0][node]
            left_nodes.append(node)
            left_relations.append(relation)
        nodes = list(reversed(left_nodes))
        relations = list(reversed(left_relations))
        node = meeting
        while node != target:
            node, relation = parents[1][node]
            nodes.append(node)
            relations.append(relation)
        # Parent trees may overlap more than once. Erase closed subwalks;
        # surviving steps keep their exact original relation identifiers.
        simple, simple_relations, positions = [nodes[0]], [], {nodes[0]: 0}
        for node, relation in zip(nodes[1:], relations):
            if node in positions:
                index = positions[node]
                for removed in simple[index + 1:]:
                    del positions[removed]
                del simple[index + 1:]
                del simple_relations[index:]
            else:
                positions[node] = len(simple)
                simple.append(node)
                simple_relations.append(relation)
        return simple, simple_relations

    def _target_traverse(self, anchors, hops, *, target, audit):
        source = anchors[0]
        two_sided = self.mode == 'bidirectional'
        roots = (source, target) if two_sided else (source,)
        visited = set(roots[:self.MAX_VISITED_NODES])
        distances = [{source: 0}, {target: 0}]
        parents = [{}, {}]
        frontier = [[source], [target] if two_sided else []]
        radii = [0, 0]
        expanded = [0, 0]
        touched = set()
        rows_examined = entries_considered = meeting_checks = 0
        candidate = None
        reason = 'node_budget_exhausted' if len(visited) < len(roots) else None
        pending = sum(map(len, frontier))

        while reason is None:
            if not frontier[0] or (two_sided and not frontier[1]):
                reason = 'target_search_exhausted'
                break
            if sum(radii) >= hops:
                reason = 'target_hop_bound_finished'
                break
            side = (0 if not two_sided or len(frontier[0]) <= len(frontier[1]) else 1)
            next_frontier = []
            layer = frontier[side]
            for layer_index, node in enumerate(layer):
                expanded[side] += 1
                for neighbor, relation in self._adjacency[node]:
                    entries_considered += 1
                    # Match frozen radius charging: repeated and already-seen
                    # relations still incur every row encounter.
                    for _row in self._relations[relation]:
                        if rows_examined == self.MAX_EDGE_ROWS:
                            reason = 'edge_budget_exhausted'
                            break
                        rows_examined += 1
                        touched.add(relation)
                    if reason:
                        break
                    if neighbor not in distances[side]:
                        if neighbor not in visited and len(visited) == self.MAX_VISITED_NODES:
                            reason = 'node_budget_exhausted'
                            break
                        visited.add(neighbor)
                        distances[side][neighbor] = distances[side][node] + 1
                        parents[side][neighbor] = (node, relation)
                        next_frontier.append(neighbor)
                    meeting_checks += 1
                    if neighbor in distances[1 - side]:
                        nodes, relations = self._joined_path(source, target, neighbor, parents)
                        if len(relations) <= hops:
                            candidate = (nodes, relations)
                            reason = 'target_witness_found'
                            break
                if reason:
                    # Interrupted current expansion plus untouched layer and
                    # admitted next-layer nodes are still pending radius work.
                    pending = (len(layer) - layer_index + len(next_frontier)
                               + (len(frontier[1 - side]) if two_sided else 0))
                    break
            if reason:
                break
            frontier[side] = sorted(next_frontier)
            radii[side] += 1
            pending = sum(map(len, frontier))

        # Return only the parent chain needed by the frozen extraction code.
        # Absence remains conservative: no targeted stop certifies an anchor's
        # complete radius, even if the two-sided bounded question was exhausted.
        result_distances = {source: {source: 0}}
        result_parents = {source: {}}
        if candidate:
            nodes, relations = candidate
            for depth, (previous, node, relation) in enumerate(
                    zip(nodes, nodes[1:], relations), 1):
                result_distances[source][node] = depth
                result_parents[source][node] = (previous, relation)
        audit.update(termination_reason=reason, completed_ball_radii=radii,
                     per_side_discovered=[len(d) for d in distances] if two_sided else [len(distances[0]), 0],
                     per_side_expanded=expanded, adjacency_entries_considered=entries_considered,
                     meeting_checks=meeting_checks, shortest_claim=False,
                     path_hops=len(candidate[1]) if candidate else None)
        work = dict(visited_nodes=len(visited), edge_rows_examined=rows_examined,
                    canonical_relations_examined=len(touched))
        state = dict(complete=False, reason=reason, pending_nodes=pending, boundary_nodes=0)
        return result_distances, result_parents, work, dict(
            **state, by_anchor=[dict(anchor_id=source, **state)])
