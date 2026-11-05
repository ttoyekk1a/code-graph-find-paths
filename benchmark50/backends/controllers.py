"""Deterministic graph controllers using only public questions and delivered packets."""

from collections import defaultdict, deque


def _canonical(node):
    return node.rsplit(";", 1)[-1]


def _edge(a, b):
    return tuple(sorted((a, b)))


def _paths(question, adjacency):
    start, target = question["start"], question["target"]
    max_hops = question["max_hops"]
    paths = []

    # Reverse distance bounds avoid exploring prefixes that cannot reach target.
    distances = {target: 0}
    frontier = deque([target])
    while frontier:
        node = frontier.popleft()
        if distances[node] == max_hops:
            continue
        for neighbor in adjacency.get(node, ()):
            if neighbor not in distances:
                distances[neighbor] = distances[node] + 1
                frontier.append(neighbor)

    def visit(path):
        node = path[-1]
        if node == target:
            paths.append(path)
            return
        remaining = max_hops - len(path) + 1
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in path and distances.get(neighbor, max_hops + 1) < remaining:
                visit([*path, neighbor])

    if start in distances:
        visit([start])
    return paths


def _request(invoke, name, args):
    try:
        response = invoke(name, args)
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"
    if not isinstance(response, dict):
        return None, "invalid_response"
    if response.get("status") == "error":
        return None, f"{response.get('error_type', 'tool_error')}: {response.get('error_message', '')}"
    return response, None


class _Run:
    def __init__(self, question, invoke):
        self.question = question
        self.invoke = invoke
        self.adjacency = defaultdict(set)
        self.edges = set()
        self.attempts = 0
        self.prefixes = []
        self.snapshot()

    def add_edge(self, a, b):
        if a != b:
            self.edges.add(_edge(a, b))
            self.adjacency[a].add(b)
            self.adjacency[b].add(a)

    def query(self, name, args):
        self.attempts += 1
        return _request(self.invoke, name, args)

    def snapshot(self):
        self.prefixes.append(dict(query_attempts=self.attempts,
                                  paths=_paths(self.question, self.adjacency)))

    def finish(self, reason=None, paths=None):
        complete = reason is None
        answer = dict(paths=self.prefixes[-1]["paths"] if paths is None else paths,
                      complete=complete)
        _, submit_error = _request(self.invoke, "submit_graph_answer", answer)
        stop_reason = reason or "complete"
        if submit_error:
            stop_reason += f"; submit_error: {submit_error}"
        return dict(answer=answer, complete_supported=complete,
                    evidence_edges=[list(edge) for edge in sorted(self.edges)],
                    query_attempts=self.attempts, stop_reason=stop_reason,
                    prefixes=self.prefixes)


def _get_neighbors(run, node, max_neighbors):
    response, failure = run.query("get_code_neighbors", dict(node=node, max_neighbors=max_neighbors))
    neighbors = []
    if failure is None:
        values = response.get("neighbors")
        response_node = response.get("node")
        if (response.get("status") != "ok"
                or not isinstance(response_node, str) or _canonical(response_node) != node
                or not isinstance(values, list)
                or any(not isinstance(value, str) or not _canonical(value) for value in values)
                or response.get("count", len(values)) != len(values)):
            failure = "invalid_neighbor_response"
        else:
            truncated = response.get("is_truncated") or response.get("truncated")
            # The SDK can clip the only returned identifier mid-string.
            if not truncated or len(values) > 1:
                neighbors = sorted({_canonical(value) for value in values})
                for neighbor in neighbors:
                    run.add_edge(node, neighbor)
            if truncated:
                failure = "neighbor_response_truncated"
            elif len(values) >= max_neighbors:
                failure = "neighbor_limit"
    run.snapshot()
    return neighbors, failure


def run_baseline(question, invoke, max_queries=128, max_neighbors=20000):
    """Observe both endpoint balls, then enumerate all paths in their edge union."""
    run = _Run(question, invoke)
    start, target = question["start"], question["target"]
    if start == target:
        return run.finish()

    first_radius = question["max_hops"] // 2
    radii = (first_radius, question["max_hops"] - first_radius)
    roots = (start, target)
    frontiers = [deque([(root, 0)] if radius else ()) for root, radius in zip(roots, radii)]
    seen = [{root} for root in roots]
    cache = {}

    while any(frontiers):
        side = min((index for index, queue in enumerate(frontiers) if queue),
                   key=lambda index: (frontiers[index][0][1], len(frontiers[index]), index))
        node, depth = frontiers[side].popleft()
        if node not in cache:
            if run.attempts >= max_queries:
                return run.finish("query_limit")
            neighbors, failure = _get_neighbors(run, node, max_neighbors)
            if failure is not None:
                return run.finish(failure)
            cache[node] = neighbors
        if depth + 1 < radii[side]:
            for neighbor in cache[node]:
                if neighbor not in seen[side]:
                    seen[side].add(neighbor)
                    frontiers[side].append((neighbor, depth + 1))
    return run.finish()


def _lookup(table, index):
    if type(index) is not int or index < 0:
        raise ValueError("invalid table index")
    return table[index]


def _decode_candidate(response):
    nodes = response["nodes"]
    if not isinstance(nodes, list) or any(not isinstance(node, str) or not node for node in nodes):
        raise ValueError("invalid node table")
    pair, = response["pairs"]
    edges = []
    for edge in response["edges"]:
        first, second = edge["nodes"]
        edges.append((_lookup(nodes, first), _lookup(nodes, second)))
    paths = []
    for path in pair.get("paths", []):
        paths.append([_lookup(nodes, index) for index in path["nodes"]])
    return edges, paths, pair


def run_candidate(question, invoke):
    """Submit the path sequences and completeness claim from the candidate packet."""
    run = _Run(question, invoke)
    response, failure = run.query("graph_find_paths", dict(source_nodes=[question["start"]],
                                                           target_nodes=[question["target"]],
                                                           max_hops=question["max_hops"]))
    paths = []
    if failure is None:
        try:
            edges, paths, pair = _decode_candidate(response)
        except (KeyError, TypeError, ValueError, IndexError) as error:
            failure = f"invalid_candidate_response: {error}"
        else:
            for a, b in edges:
                run.add_edge(a, b)
            if response.get("complete") is not True or pair.get("complete") is not True:
                reasons = response.get("truncation") or [pair.get("status", "unknown")]
                failure = "candidate_incomplete: " + ", ".join(reasons)
    run.snapshot()
    return run.finish(failure, paths)
