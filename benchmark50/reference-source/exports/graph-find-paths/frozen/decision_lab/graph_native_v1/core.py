"""Stored-adjacency witnesses, without source interpretation or execution.

A path is one deterministic shortest adjacency witness per anchor/target pair,
not a directed call chain or a claim about runtime behavior. Cold loading,
validation and canonicalization are reported separately from query work.
"""
from collections import deque
from copy import deepcopy
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
from time import perf_counter


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON object key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('non-JSON numeric constant: ' + value)


def _finite_float(value):
    number = float(value)
    if not isfinite(number):
        raise ValueError('JSON number exceeds finite float range')
    return number


class GraphWitnessSession:
    """Read a local graph once; bound each query independently.

    Node metadata, including stored text, is available through ``nodes`` as a
    defensive copy in either mode. No current-source verification is performed.
    Constructor/input errors raise ValueError (filesystem failures remain OSError).
    Responses fit ``len(json.dumps(response))``; limits below 512 are rejected
    because even a truthful work-accounting error needs space.
    """

    MAX_FILE_BYTES = 16 * 1024 * 1024
    MAX_NODES = 20000
    MAX_EDGES = 100000
    MAX_VISITED_NODES = 160
    MAX_EDGE_ROWS = 800
    MAX_PATHS = 8
    MAX_MATCHES = 8

    def __init__(self, graph_path, expected_sha256, *, topology_enabled=True, output_limit=8000):
        started = perf_counter()
        if type(topology_enabled) is not bool:
            raise ValueError('InvalidTopologyMode: expected bool')
        if type(output_limit) is not int or not 512 <= output_limit <= 8000:
            raise ValueError('InvalidOutputLimit: expected integer 512..8000')
        if not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
            raise ValueError('InvalidSHA256: expected 64 hexadecimal characters')
        self.topology_enabled = topology_enabled
        self.output_limit = output_limit
        self.graph_path = str(Path(graph_path))
        with Path(graph_path).open('rb') as stream:
            raw = stream.read(self.MAX_FILE_BYTES + 1)
        if len(raw) > self.MAX_FILE_BYTES:
            raise ValueError('FileCapExceeded')
        self.graph_sha256 = hashlib.sha256(raw).hexdigest()
        if self.graph_sha256 != expected_sha256.lower():
            raise ValueError('SHA256Mismatch')
        try:
            graph = json.loads(raw, object_pairs_hook=_strict_object,
                parse_constant=_invalid_constant, parse_float=_finite_float)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError('MalformedGraph: invalid JSON') from exc
        if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list):
            raise ValueError('MalformedGraph: nodes must be an array')
        if 'edges' in graph and 'links' in graph:
            raise ValueError('MalformedGraph: ambiguous edges and links arrays')
        edge_field = 'edges' if 'edges' in graph else 'links'
        rows = graph.get(edge_field)
        if not isinstance(rows, list):
            raise ValueError('MalformedGraph: edges/links must be an array')
        if len(graph['nodes']) > self.MAX_NODES:
            raise ValueError('NodeCapExceeded')
        if len(rows) > self.MAX_EDGES:
            raise ValueError('EdgeCapExceeded')
        self._nodes = {}
        for node in graph['nodes']:
            if (not isinstance(node, dict) or not isinstance(node.get('id'), str)
                    or not node['id'] or node['id'] in self._nodes):
                raise ValueError('MalformedGraph: invalid or duplicate node identity')
            self._nodes[node['id']] = node
        self._ids = tuple(sorted(self._nodes))
        grouped = {}
        for index, row in enumerate(rows):
            if (not isinstance(row, dict) or not isinstance(row.get('source'), str)
                    or not isinstance(row.get('target'), str)
                    or row['source'] not in self._nodes or row['target'] not in self._nodes):
                raise ValueError('MalformedGraph: edge endpoints must be declared node IDs')
            pair = tuple(sorted((row['source'], row['target'])))
            grouped.setdefault(pair, []).append(dict(index=index, source=row['source'],
                target=row['target'], key=row.get('key'), type=row.get('type')))
        self._relations = {}
        self._adjacency = {node_id: [] for node_id in self._ids}
        for index, pair in enumerate(sorted(grouped)):
            relation_id = 'r' + str(index)
            self._relations[relation_id] = tuple(grouped[pair])
            a, b = pair
            self._adjacency[a].append((b, relation_id))
            if a != b:
                self._adjacency[b].append((a, relation_id))
        for neighbors in self._adjacency.values():
            neighbors.sort()
        self._setup = dict(file_bytes=len(raw), nodes=len(self._nodes), edge_rows=len(rows),
            canonical_relations=len(grouped), edge_field=edge_field,
            elapsed_seconds=perf_counter() - started,
            caps=dict(file_bytes=self.MAX_FILE_BYTES, nodes=self.MAX_NODES, edges=self.MAX_EDGES))

    @property
    def nodes(self):
        """All original node attributes, including graph-stored text; copied."""
        return deepcopy(self._nodes)

    @staticmethod
    def _validate_query(sources, targets, hops):
        if type(hops) is not int or not 1 <= hops <= 4:
            raise ValueError('InvalidHops: expected integer 1..4, excluding bool')
        for label, values, maximum in [('sources', sources, 3), ('targets', targets, 8)]:
            if (not isinstance(values, list) or not 1 <= len(values) <= maximum
                    or any(not isinstance(v, str) or not v for v in values)):
                raise ValueError('InvalidNodes: ' + label + ' must be a bounded list of nonempty strings')

    def _resolve(self, value):
        # Exact IDs take precedence; suffixes begin at a namespace/path boundary.
        if value in self._nodes:
            return dict(input=value, status='exact')
        matches = [node_id for node_id in self._ids if node_id.endswith(value)
                   and len(node_id) > len(value) and node_id[-len(value)-1] in './:#\\']
        if len(matches) == 1:
            return dict(input=value, status='suffix', node_id=matches[0])
        if matches:
            return dict(input=value, status='AmbiguousNode', matches=matches[:self.MAX_MATCHES],
                        match_count=len(matches), matches_omitted=max(0, len(matches)-self.MAX_MATCHES))
        return dict(input=value, status='NodeNotInGraph')

    @staticmethod
    def _resolved_id(resolution):
        if resolution['status'] in ('exact', 'suffix'):
            return resolution.get('node_id', resolution['input'])
        return None

    def graph_find_paths(self, source_nodes: list[str], target_nodes: list[str], max_hops: int = 4) -> dict:
        """At most eight complete, shortest stored-adjacency witnesses."""
        return self._query(source_nodes, target_nodes, max_hops, compare=False)

    def graph_compare_locations(self, anchor_nodes: list[str], candidate_nodes: list[str], max_hops: int = 4) -> dict:
        """Compare only positive anchor connections after comparable completed work."""
        return self._query(anchor_nodes, candidate_nodes, max_hops, compare=True)

    def _traverse(self, anchors, hops):
        """One interleaved BFS queue and invocation-global counters for all anchors."""
        visited = set(anchors)
        distances = {a: {a: 0} for a in anchors}
        parents = {a: {} for a in anchors}
        queue = deque((a, a, 0) for a in anchors)
        pending = {a: 1 for a in anchors}
        boundary = {a: 0 for a in anchors}
        touched_relations = set()
        edge_rows = 0
        reason = None
        if not self.topology_enabled:
            return distances, parents, dict(visited_nodes=len(visited), edge_rows_examined=0,
                canonical_relations_examined=0), dict(complete=True, reason='topology_disabled',
                pending_nodes=0, boundary_nodes=0, by_anchor=[dict(anchor_id=a, complete=True,
                reason='topology_disabled', pending_nodes=0, boundary_nodes=0) for a in anchors])
        while queue and reason is None:
            anchor, node, depth = queue.popleft()
            pending[anchor] -= 1
            if depth == hops:
                boundary[anchor] += 1
                continue
            for neighbor, relation_id in self._adjacency[node]:
                # Count every actual row examination, including repeated encounters
                # across anchor searches. A partial relation yields no path witness.
                rows = self._relations[relation_id]
                for _row in rows:
                    if edge_rows == self.MAX_EDGE_ROWS:
                        reason = 'edge_budget_exhausted'
                        break
                    edge_rows += 1
                    touched_relations.add(relation_id)
                if reason:
                    break
                if neighbor in distances[anchor]:
                    continue
                if neighbor not in visited and len(visited) == self.MAX_VISITED_NODES:
                    reason = 'node_budget_exhausted'
                    break
                visited.add(neighbor)
                distances[anchor][neighbor] = depth + 1
                parents[anchor][neighbor] = (node, relation_id)
                queue.append((anchor, neighbor, depth + 1))
                pending[anchor] += 1
            if reason:
                # This expansion was interrupted, even if no queue item remains.
                pending[anchor] += 1
        by_anchor = [dict(anchor_id=a, complete=pending[a] == 0,
            reason='completed_hop_bound' if pending[a] == 0 else reason,
            pending_nodes=pending[a], boundary_nodes=boundary[a]) for a in anchors]
        frontier = dict(complete=reason is None, reason=reason or 'completed_hop_bound',
            pending_nodes=sum(pending.values()), boundary_nodes=sum(boundary.values()), by_anchor=by_anchor)
        return distances, parents, dict(visited_nodes=len(visited), edge_rows_examined=edge_rows,
            canonical_relations_examined=len(touched_relations)), frontier

    def _path(self, anchor, target, parents, path_id):
        nodes = [target]
        steps = []
        node = target
        while node != anchor:
            previous, relation_id = parents[anchor][node]
            steps.append(dict(**{'from': previous, 'to': node}, relation_id=relation_id,
                              edge_rows=deepcopy(list(self._relations[relation_id]))))
            nodes.append(previous)
            node = previous
        return dict(path_id=path_id, node_ids=list(reversed(nodes)), steps=list(reversed(steps)))

    def _query(self, sources, targets, hops, compare):
        started = perf_counter()
        self._validate_query(sources, targets, hops)
        resolution = dict(sources=[self._resolve(v) for v in sources],
                          targets=[self._resolve(v) for v in targets])
        anchor_ids = [self._resolved_id(v) for v in resolution['sources']]
        target_ids = [self._resolved_id(v) for v in resolution['targets']]
        anchors = sorted({a for a in anchor_ids if a is not None})
        distances, parents, work, frontier = self._traverse(anchors, hops)
        completeness = {entry['anchor_id']: entry['complete'] for entry in frontier['by_anchor']}
        matrix, paths, pair_paths = [], [], {}
        positive_pairs = set()
        for ai, anchor in enumerate(anchor_ids):
            for ci, target in enumerate(target_ids):
                cell = dict(anchor_index=ai, candidate_index=ci, state='unknown', path_ids=[])
                if anchor is None or target is None:
                    pass
                elif anchor == target:
                    cell['state'] = 'identity'
                elif not self.topology_enabled:
                    cell['state'] = 'topology_disabled'
                elif target in distances[anchor]:
                    pair = (anchor, target)
                    positive_pairs.add(pair)
                    if pair not in pair_paths:
                        if len(paths) < self.MAX_PATHS:
                            path_id = 'p' + str(len(paths))
                            paths.append(self._path(anchor, target, parents, path_id))
                            pair_paths[pair] = path_id
                        else:
                            pair_paths[pair] = None
                    if pair_paths[pair] is None:
                        cell['state'] = 'witnessed_omitted'
                    else:
                        cell['state'] = 'witnessed'
                        cell['path_ids'] = [pair_paths[pair]]
                else:
                    cell['state'] = 'hop_bound' if completeness[anchor] else 'budget_exhausted'
                matrix.append(cell)
        preference = dict(status='unresolved', reason='no_strict_positive_superset')
        if compare and self.topology_enabled and frontier['complete'] and None not in anchor_ids + target_ids:
            candidates = sorted(set(target_ids))
            connections = {c: {a for a in anchors if (a, c) in positive_pairs} for c in candidates}
            if len(candidates) > 1:
                winners = [c for c in candidates if all(connections[c] > connections[o]
                           for o in candidates if o != c)]
                if len(winners) == 1:
                    preference = dict(status='preferred_for_inspection', candidate_id=winners[0],
                                      reason='strict_superset_of_positive_anchor_connections')
        result = dict(graph_sha256=self.graph_sha256, traversal_semantics='stored_adjacency',
            topology_enabled=self.topology_enabled, ablation_mode='none' if self.topology_enabled else 'node_only',
            source_grounding='not_performed', text_semantics='stored_graph_text', max_hops=hops,
            resolution=resolution, paths=paths, matrix=matrix, preference=preference, work=work,
            frontier=frontier, setup=deepcopy(self._setup),
            omissions=dict(paths=len(positive_pairs)-len(paths)),
            limitations=['Adjacency only; no call direction, dataflow, causality or runtime reachability.',
                         'Identity is not edge support. Missing paths do not reject a hypothesis.',
                         'One shortest witness per pair; source grounding not performed.'])
        return self._bound(result, started)

    def _bound(self, result, started):
        """Remove whole optional paths only; otherwise give a truthful bounded error."""
        while True:
            if result['omissions']['paths']:
                result['preference'] = dict(status='unresolved', reason='witnesses_omitted_from_output')
            result['work']['elapsed_seconds'] = perf_counter() - started
            if len(json.dumps(result)) <= self.output_limit:
                return result
            if not result['paths']:
                break
            removed = result['paths'].pop()
            result['omissions']['paths'] += 1
            for cell in result['matrix']:
                if removed['path_id'] in cell['path_ids']:
                    cell['path_ids'] = []
                    cell['state'] = 'witnessed_omitted'
        # No IDs are shortened. Required resolution/matrix metadata did not fit,
        # so return no matrix or path references instead of a misleading fragment.
        error = dict(error='OutputLimitExceeded', graph_sha256=self.graph_sha256,
            traversal_semantics='stored_adjacency', source_grounding='not_performed',
            topology_enabled=self.topology_enabled, ablation_mode=result['ablation_mode'],
            frontier_complete=result['frontier']['complete'],
            pending_nodes=result['frontier']['pending_nodes'], work=result['work'],
            setup_elapsed_seconds=self._setup['elapsed_seconds'])
        error['work']['elapsed_seconds'] = perf_counter() - started
        # All fields are fixed-size scalars bounded by constructor caps; fits 512.
        return error
