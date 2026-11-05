"""Offline grading for the shared all-path submission contract."""


def grade(question, answer, expected_paths, evidence_edges, complete_supported):
    """Score a JSON-shaped submission against the reference and delivered edges."""
    expected = {tuple(path) for path in expected_paths}
    edges = {frozenset(pair) for pair in evidence_edges}
    start, target, max_hops = (question[key] for key in ("start", "target", "max_hops"))
    submitted = answer.get("paths") if isinstance(answer, dict) else None
    claim = answer.get("complete") if isinstance(answer, dict) else None
    shape_valid = (
        isinstance(answer, dict)
        and answer.keys() == {"paths", "complete"}
        and type(claim) is bool
        and isinstance(submitted, list)
    )
    paths = submitted if isinstance(submitted, list) else []
    seen = set()
    matches = set()
    extra = invalid = unsupported = duplicate = 0
    for path in paths:
        if not isinstance(path, list) or any(not isinstance(node, str) for node in path):
            shape_valid = False
            invalid += 1
            continue
        key = tuple(path)
        if key in seen:
            duplicate += 1
            continue
        seen.add(key)
        if (
            not path or any(not node.strip() for node in path)
            or path[0] != start or path[-1] != target
            or len(path) - 1 > max_hops or len(set(path)) != len(path)
        ):
            invalid += 1
            continue
        supported = all(frozenset(pair) in edges for pair in zip(path, path[1:]))
        if not supported:
            unsupported += 1
        if key not in expected:
            extra += 1
        elif supported:
            matches.add(key)

    exact = shape_valid and not (extra or invalid or unsupported or duplicate) and matches == expected
    certified = exact and claim is True and complete_supported is True
    return {
        "exact_set_success": exact,
        "certified_success": certified,
        "recall": len(matches) / len(expected) if expected else 1.0,
        "precision": len(matches) / len(paths) if paths else float(shape_valid and not expected),
        "matched_path_count": len(matches),
        "expected_path_count": len(expected),
        "submitted_path_count": len(paths),
        "extra_path_count": extra,
        "invalid_path_count": invalid,
        "unsupported_path_count": unsupported,
        "duplicate_path_count": duplicate,
        "false_complete_claim": claim is True and not certified,
        "complete_claim": claim is True,
        "answer_shape_valid": shape_valid,
    }
