"""Independent meet-in-the-middle oracle over raw graph JSON."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter


COHORTS = (
    ("original32", "path-study-expanded-20261103", 32),
    ("followup18", "path-study-followup-20261103", 18),
)


def graph_adjacency(nodes, rows):
    adjacency = {node: set() for node in nodes}
    for row in rows:
        source, target = row["source"], row["target"]
        adjacency[source].add(target)
        adjacency[target].add(source)
    return adjacency


def resolve_endpoint(nodes, endpoint):
    if endpoint in nodes:
        return endpoint, "exact"
    matches = sorted(node for node in nodes if node.endswith("." + endpoint))
    if len(matches) != 1:
        raise ValueError(f"{endpoint!r} resolves to {len(matches)} suffix IDs: {matches}")
    return matches[0], "suffix"


def halves_by_end(adjacency, endpoint, depth):
    halves = defaultdict(list)
    frontier = [(endpoint,)]
    for _ in range(depth + 1):
        next_frontier = []
        for half in frontier:
            last = half[-1]
            halves[last].append(half)
            if len(half) <= depth:
                next_frontier.extend(half + (neighbor,) for neighbor in adjacency[last]
                                     if neighbor not in half)
        frontier = next_frontier
    return halves


def all_paths(adjacency, start, target, max_hops):
    if not 1 <= max_hops <= 4:
        raise ValueError("max_hops must be between 1 and 4")
    depth = min(2, max_hops)
    left = halves_by_end(adjacency, start, depth)
    right = halves_by_end(adjacency, target, depth)
    paths = set()
    join_candidates = 0
    for middle in left.keys() & right.keys():
        for prefix in left[middle]:
            for suffix in right[middle]:
                join_candidates += 1
                path = prefix + suffix[-2::-1]
                if len(path) - 1 <= max_hops and len(set(path)) == len(path):
                    paths.add(path)
    return sorted(paths), {
        "start_halves": sum(map(len, left.values())),
        "target_halves": sum(map(len, right.values())),
        "join_candidates": join_candidates,
    }


def run(graphs_dir, evidence_dir):
    start_time = perf_counter()
    results = []
    cohort_inputs = {}
    for cohort, directory, expected_count in COHORTS:
        question_file = evidence_dir / directory / "dataset" / "questions.jsonl"
        raw_questions = question_file.read_bytes()
        questions = [json.loads(line) for line in raw_questions.splitlines()]
        if len(questions) != expected_count:
            raise ValueError(f"Unexpected cohort size for {cohort}: {len(questions)}")
        if len({question["query_id"] for question in questions}) != expected_count:
            raise ValueError(f"Duplicate query IDs in {cohort}")
        cohort_inputs[cohort] = {
            "questions_file": str(question_file.relative_to(evidence_dir)),
            "questions_sha256": sha256(raw_questions).hexdigest(),
            "questions": expected_count,
        }
        for question, raw_question in zip(questions, raw_questions.splitlines(keepends=True)):
            query_time = perf_counter()
            raw_graph = (graphs_dir / question["graph_file"]).read_bytes()
            digest = sha256(raw_graph).hexdigest()
            if digest != question["graph_raw_sha256"]:
                raise ValueError(f"Unexpected graph SHA-256 for {question['query_id']}: {digest}")
            graph = json.loads(raw_graph)
            nodes = [node["id"] for node in graph["nodes"]]
            if len(set(nodes)) != len(nodes):
                raise ValueError(f"Duplicate raw node IDs in {question['graph_file']}")
            rows = graph["edges"]
            adjacency = graph_adjacency(nodes, rows)
            source, source_resolution = resolve_endpoint(adjacency, question["start"])
            target, target_resolution = resolve_endpoint(adjacency, question["target"])
            enumeration_time = perf_counter()
            paths, cost = all_paths(adjacency, source, target, question["max_hops"])
            cost["enumeration_seconds"] = perf_counter() - enumeration_time
            results.append({
                **question,
                "cohort": cohort,
                "question_line_sha256": sha256(raw_question).hexdigest(),
                "source_id": source,
                "target_id": target,
                "source_resolution": source_resolution,
                "target_resolution": target_resolution,
                "graph_nodes": len(nodes),
                "graph_edge_rows": len(rows),
                "paths": paths,
                "path_count": len(paths),
                "paths_by_hops": dict(sorted(Counter(len(path) - 1 for path in paths).items())),
                "cost": {**cost, "query_seconds": perf_counter() - query_time},
            })

    return {
        "schema_version": 1,
        "graphs_dir": str(graphs_dir),
        "cohort_inputs": cohort_inputs,
        "summary": {
            "queries": len(results),
            "distinct_graphs": len({result["graph_raw_sha256"] for result in results}),
            "path_count": sum(result["path_count"] for result in results),
            "elapsed_seconds": perf_counter() - start_time,
        },
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graphs_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    evidence_dir = Path(__file__).resolve().parent.parent
    report = run(args.graphs_dir.resolve(), evidence_dir)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
