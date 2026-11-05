"""Enumerate node-simple paths in validated, undirected stored adjacency."""

from collections import deque
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import ModuleType


_FROZEN_SHA256 = "50346a0791e196155b4bc82cf8211f9b1992139c594375d24955718f7ad32bec"
_FROZEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "exports/graph-find-paths/frozen/decision_lab/graph_native_v1/core.py"
)


def _load_frozen():
    source = _FROZEN_PATH.read_bytes()
    if sha256(source).hexdigest() != _FROZEN_SHA256:
        raise ValueError("FrozenLoaderSHA256Mismatch")
    module = ModuleType("_all_paths_frozen_graph_loader")
    module.__file__ = str(_FROZEN_PATH)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module.GraphWitnessSession


_GraphWitnessSession = _load_frozen()


class _SearchLimit(Exception):
    pass


class _Invocation:
    def __init__(self, rows, max_hops, limits):
        self.rows = rows
        self.max_hops = max_hops
        self.limits = limits
        self.work = dict(edge_rows_examined=0, dfs_states=0, unique_paths=0)
        self.truncation = []
        self.adjacency = None
        self.edge_rows = None
        self.distances = {}

    def charge(self, counter, reason, count=1):
        remaining = self.limits[counter] - self.work[counter]
        if count > remaining:
            # Batches represent original rows, including the partial batch at the limit.
            self.work[counter] += remaining
            if reason not in self.truncation:
                self.truncation.append(reason)
            raise _SearchLimit
        self.work[counter] += count

    def build_adjacency(self):
        grouped = {}
        for index, row in enumerate(self.rows):
            self.charge("edge_rows_examined", "edge_row_limit")
            a, b = sorted((row["source"], row["target"]))
            if a != b:
                grouped.setdefault((a, b), []).append(index)
        adjacency = {}
        for edge in grouped:
            a, b = edge
            adjacency.setdefault(a, []).append((b, edge))
            adjacency.setdefault(b, []).append((a, edge))
        for neighbors in adjacency.values():
            neighbors.sort()
        self.adjacency = adjacency
        self.edge_rows = grouped

    def neighbors(self, node):
        for neighbor, edge in self.adjacency.get(node, ()):
            self.charge("edge_rows_examined", "edge_row_limit", len(self.edge_rows[edge]))
            yield neighbor, edge

    def target_distances(self, target):
        if target in self.distances:
            return self.distances[target]
        distances = {target: 0}
        pending = deque([target])
        while pending:
            node = pending.popleft()
            depth = distances[node]
            if depth < self.max_hops:
                for neighbor, _ in self.neighbors(node):
                    if neighbor not in distances:
                        distances[neighbor] = depth + 1
                        pending.append(neighbor)
        self.distances[target] = distances
        return distances

    def find(self, source, target):
        paths = []

        def visit(nodes, edges):
            self.charge("dfs_states", "dfs_state_limit")
            node = nodes[-1]
            if node == target:
                self.charge("unique_paths", "path_limit")
                paths.append((nodes, edges))
                return
            remaining = self.max_hops - len(edges)
            if remaining:
                for neighbor, edge in self.neighbors(node):
                    if neighbor not in nodes and distances.get(neighbor, self.max_hops + 1) < remaining:
                        visit(nodes + (neighbor,), edges + (edge,))

        try:
            if source != target:
                if self.adjacency is None:
                    self.build_adjacency()
                distances = self.target_distances(target)
                if source not in distances:
                    return paths, True
            visit((source,), ())
        except _SearchLimit:
            return paths, False
        return paths, True


class AllPathsSession:
    """Load one validated graph and run independently bounded all-path queries."""

    def __init__(
        self, graph_path, expected_sha256, *, max_edge_rows=2_000_000,
        max_dfs_states=500_000, max_paths=100_000, output_limit=1_000_000,
    ):
        limits = dict(edge_rows_examined=max_edge_rows, dfs_states=max_dfs_states, unique_paths=max_paths)
        if any(type(value) is not int or value < 0 for value in limits.values()):
            raise ValueError("Search limits must be nonnegative integers")
        if type(output_limit) is not int or output_limit < 4096:
            raise ValueError("Output limit must be an integer >=4096")
        self._graph = _GraphWitnessSession(graph_path, expected_sha256)
        with Path(graph_path).open("rb") as stream:
            raw = stream.read(self._graph.MAX_FILE_BYTES + 1)
        if sha256(raw).hexdigest() != self._graph.graph_sha256:
            raise ValueError("SHA256Mismatch")
        # The frozen resolver stores projected rows. Preserve arbitrary original attributes.
        graph = json.loads(raw)
        self._rows = graph["edges"] if "edges" in graph else graph["links"]
        self._limits = limits
        self._output_limit = output_limit

    def graph_find_paths(self, source_nodes: list[str], target_nodes: list[str], max_hops: int = 4) -> dict:
        """Return bounded, deduplicated stored-adjacency paths for every ordered pair."""
        if type(max_hops) is not int or not 1 <= max_hops <= 16:
            raise ValueError("InvalidHops: expected integer 1..16, excluding bool")
        # Reuse unchanged node validation; the frozen v1 validator only accepts 1..4.
        self._graph._validate_query(source_nodes, target_nodes, 4)
        resolution = {
            "sources": [self._graph._resolve(value) for value in source_nodes],
            "targets": [self._graph._resolve(value) for value in target_nodes],
        }
        sources = [self._graph._resolved_id(item) for item in resolution["sources"]]
        targets = [self._graph._resolved_id(item) for item in resolution["targets"]]
        work = _Invocation(self._rows, max_hops, self._limits)
        cached = {}
        pairs = []
        for i, source in enumerate(sources):
            for j, target in enumerate(targets):
                key = (source, target)
                if None in key:
                    pairs.append((i, j, None, False))
                    continue
                if key not in cached:
                    cached[key] = work.find(source, target)
                paths, search_complete = cached[key]
                pairs.append((i, j, paths, search_complete))
        return deepcopy(self._bounded_packet(work, pairs, resolution))

    def _bounded_packet(self, work, pairs, resolution):
        total_found = sum(len(paths or ()) for _, _, paths, _ in pairs)

        def packet(path_limit, resolution_mode):
            nodes, edges = {}, {}
            node_table, edge_table, records = [], [], []

            def node_index(node):
                if node not in nodes:
                    nodes[node] = len(node_table)
                    node_table.append(node)
                return nodes[node]

            def edge_index(edge):
                if edge not in edges:
                    edges[edge] = len(edge_table)
                    edge_table.append(dict(
                        nodes=[node_index(node) for node in edge],
                        rows=[dict(index=i, row=self._rows[i]) for i in work.edge_rows[edge]],
                    ))
                return edges[edge]

            remaining = path_limit
            for i, j, paths, searched in pairs:
                found = len(paths or ())
                returned = min(found, remaining)
                remaining -= returned
                if paths is None:
                    status = "unresolved"
                elif returned:
                    status = "connected"
                elif searched and not found:
                    status = "no_path"
                else:
                    status = "incomplete"
                record = dict(
                    source_index=i, target_index=j, status=status,
                    found_count=found, returned_count=returned,
                    search_complete=searched, output_complete=(found == returned),
                    complete=(searched and found == returned),
                    paths=[
                        dict(nodes=[node_index(node) for node in sequence],
                             edges=[edge_index(edge) for edge in relations])
                        for sequence, relations in (paths or ())[:returned]
                    ],
                )
                if resolution_mode == "minimal":
                    # All pair positions are implicit in the source-major grid.
                    del record["source_index"], record["target_index"]
                    if not returned:
                        del record["paths"]
                records.append(record)

            searched = all(record["search_complete"] for record in records)
            output_complete = path_limit == total_found and resolution_mode == "full"
            truncation = list(work.truncation)
            if not output_complete:
                truncation.append("output_limit")
            details = resolution
            if resolution_mode != "full":
                details = {name: [item["status"] for item in items] for name, items in resolution.items()}
            return dict(
                version="graph-find-paths", graph_sha256=self._graph.graph_sha256,
                semantics="stored_undirected_adjacency", source_grounding="not_performed",
                max_hops=work.max_hops, resolution=details, resolution_mode=resolution_mode,
                nodes=node_table, edges=edge_table, pairs=records, work=work.work,
                truncation=truncation, search_complete=searched,
                output_complete=output_complete, complete=searched and output_complete,
            )

        mode = "full"
        best = packet(0, mode)
        for fallback in ("statuses", "minimal"):
            if len(json.dumps(best)) <= self._output_limit:
                break
            mode = fallback
            best = packet(0, mode)
        # Even a single path needs at least 25 JSON characters. Each prefix only
        # grows: the largest metadata shrink from adding a path is smaller than that.
        low, high = 1, min(total_found, self._output_limit // 25)
        while low <= high:
            middle = (low + high) // 2
            candidate = packet(middle, mode)
            if len(json.dumps(candidate)) <= self._output_limit:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best


def make_graph_find_paths(graph_path, expected_sha256, **limits):
    """Build a callable with the public graph_find_paths signature."""
    return AllPathsSession(graph_path, expected_sha256, **limits).graph_find_paths
