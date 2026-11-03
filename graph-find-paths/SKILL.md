---
name: graph-find-paths
description: Use when integrating, reproducing, or using the frozen D graph_find_paths tool over original SWE-gemma competition graph JSON assets.
---

# graph-find-paths

This is a reference skill for the **D tool: bidirectional search, full witness output**. Its registered function name is exactly `graph_find_paths`. The folder/skill name is exactly `graph-find-paths`; these are two different namespaces. Do not rename either.

For exact experiment replication, use the saved benchmark system/schema as the solver configuration. Use this skill for integration and tool-operation guidance.

## Reproduce or integrate

Read the [competition integration instructions](../README.md#optional-competition-reproduction), then run:

```bash
python3 -I -B scripts/verify.py
python3 -I -B scripts/verify.py --graphs-dir /absolute/path/to/original/graphs
```

Run from this skill directory. The second command requires all 18 original graph files listed in `fixtures/real-graph-replays.json`. Missing or mismatched assets fail; do not regenerate graphs or change expected hashes to pass. Fixtures are evaluator-only, never solver context.

Reuse the hash-pinned source in `frozen/` and `scripts/build_tool.py`. Do not reimplement BFS from the description. Keep the exact model-facing [schema](references/tool-schema.json). Supply an owned runtime/context to the competition builder. `verify.py` checks the frozen contracts with a synthetic context. Validate host integration through exact name/schema registration, budget accounting, full result delivery and session cleanup.

## Use the tool

Given exact stored node IDs, call:

```json
{"source_nodes":["rich.progress.SpinnerColumn.render"],"target_nodes":["rich.progress.DownloadColumn.render"],"max_hops":4}
```

Prefer one source and one target, the tested bidirectional case. Other permitted input shapes use the preserved fallback. The graph must match the active task's immutable base asset. Resolve node IDs with the host's existing navigation, then pass them to the tool.

Read `matrix`, `paths`, `resolution`, `frontier`, and `omissions` together. `partial` can contain a valid `connected` path. Accept only complete retained paths with original edge-row evidence. `unknown` pairs remain unresolved, including cases with ambiguous nodes, omitted paths or exhausted search budgets. Returned paths establish stored **undirected adjacency** within the hop bound. Use live source inspection to connect graph evidence to code edits.

Keep limits: 1–4 hops, 160 visited nodes, 800 encountered original edge rows; core 6000 and public 8000 default-JSON characters. Do not compact output, add AST edges, deduplicate input rows, retry internally, or change names/fields. A `mode: C` response is intentional legacy serialization for experimental arm D.

`submit_graph_answer` submits a path benchmark result. For code repairs, use the host's `submit_patch` interface.
