# Fast Path Search in Code Graph

Use **`graph_find_paths`** to find bounded paths in **your own code graph**, through Python, a command-line tool, or an MCP server. The default standalone runtime needs **no Kaggle account, competition SDK, model weights, Docker, or network access at query time**. Python/CLI have no third-party runtime dependencies; MCP is an optional extra.

One query returns a bounded path together with complete original edge-row evidence. Supply your existing JSON code graph and let the tool traverse its **stored undirected adjacency**.

## Install and run a complete example

Python 3.12+ is required. This public repository can be cloned without GitHub authentication. Installation requires only ordinary Python packaging tools.

```bash
git clone https://github.com/ttoyekk1a/code-graph-find-paths.git
cd code-graph-find-paths
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/code-graph-find-paths --graph examples/code-graph.json \
  query --source app.entry --target app.store
```

This is a complete copy-and-run example: no graph downloads, account credentials, or SHA placeholders. The result contains `matrix[0].state = "connected"` and the path `app.entry → app.service → app.store`. Its top-level status can be `partial`: the search stops as soon as it finds a complete witness.

To install directly from Git:

```bash
python3 -m pip install 'git+https://github.com/ttoyekk1a/code-graph-find-paths.git'
```

Install from GitHub using the commands above. The shell examples use macOS/Linux executable paths.

## Bring your own graph

Save this as `my-graph.json`, or supply the same structure from your graph exporter:

```json
{
  "nodes": [{"id": "app.entry"}, {"id": "app.service"}, {"id": "app.store"}],
  "edges": [
    {"source": "app.entry", "target": "app.service", "type": "calls"},
    {"source": "app.service", "target": "app.store", "type": "calls"}
  ]
}
```

```bash
code-graph-find-paths --graph my-graph.json \
  query --source app.entry --target app.store --max-hops 4
```

Requirements: unique, nonempty string node IDs; every edge endpoint declared in `nodes`; exactly one array named `edges` or `links`. Optional `key`/`type`, duplicate rows and reciprocal rows are retained as evidence. No sorting or rewriting of the graph file occurs. Loader limits: 16 MiB, 20,000 nodes, 100,000 edge rows. Directed graph metadata is accepted, but path traversal uses undirected adjacency.

`--sha256` is optional in standalone mode. The runtime computes and pins the supplied file's bytes automatically. To verify them against a trusted asset manifest, pass `--sha256 <trusted-hash>`; a mismatch fails. A session loads an immutable snapshot once: restart it after changing the graph file. Use one server process per graph.

## Python API

```python
from code_graph_find_paths import GraphRuntime

with GraphRuntime("my-graph.json", tool_calls=8) as graph:
    result = graph.query(
        source_nodes=["app.entry"],
        target_nodes=["app.store"],
        max_hops=4,
    )
    print(result["matrix"])
    print(result["paths"])
```

No SDK is imported and no workspace/container is created. The context manager closes the local session; using it after closure is rejected. You may pass `expected_graph_sha256="..."` to enforce provenance. `graph.schema` exposes the tool's model-facing declaration.

## Connect an agent through MCP

Install the optional transport:

```bash
.venv/bin/python -m pip install '.[mcp]'
```

Add a stdio server to your agent's MCP configuration, replacing only the executable and your graph file's absolute paths:

```json
{
  "mcpServers": {
    "code-graph-find-paths": {
      "command": "/absolute/path/code-graph-find-paths/.venv/bin/code-graph-find-paths",
      "args": ["--graph", "/absolute/path/my-graph.json", "serve"]
    }
  }
}
```

The host may need to reload its MCP configuration. It will discover exactly **`graph_find_paths`**. Example tool arguments:

```json
{"source_nodes": ["app.entry"], "target_nodes": ["app.store"], "max_hops": 4}
```

Configure the graph path and hash on the host. MCP delivers the entire result as default-serialized JSON text, without a second truncation pass. The default allowance is **8 attempts per session**, including invalid/unknown requests; set `--tool-calls 100` before `serve` for a longer agent session. For additional queries after exhaustion, start a new session with an appropriate allowance. Controller errors have `scope: controller`; query evidence retains the original format, including legacy `mode: C` and local `native` budget counters.

## Search behavior and budgets

- One to three sources and one to eight targets; exact IDs or unambiguous suffixes.
- 1–4 hops, 160 visited nodes and 800 encountered original edge rows **for the whole query**, not per endpoint pair.
- Single resolved source/target uses bidirectional search; other allowed shapes retain the radius fallback.
- Core/public limits remain 6000/8000 default-JSON characters. Only complete paths are retained.
- `partial` can contain usable `connected` paths. Treat `unknown` pairs as unresolved; a bounded absence result requires a completed search.
- Feed the tool a JSON graph from your existing code-analysis or graph-generation workflow.

## Tests without competition dependencies

After installing from this checkout:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_standalone.py' -v
# With the optional [mcp] extra installed:
.venv/bin/python scripts/standalone_mcp_smoke.py
```

These checks run directly on small user-owned graphs. The original frozen contract tests also need only the standard library:

```bash
python3 -I -B graph-find-paths/scripts/verify.py
```

## Optional competition reproduction

The frozen search implementation, schema and 18 historical replay fixtures in `graph-find-paths/`, plus `datasets/original32/`, are preserved. Internal transfer documents are excluded from the distribution; the manifest describes the retained files. The standalone engine is an explicitly documented adaptation; see [source provenance](code_graph_find_paths/_engine/PROVENANCE.json).

### Example: `get_code_neighbors` vs. `graph_find_paths`

This example compares **`get_code_neighbors`**, the built-in neighbor lookup tool provided by the [Google Gemma 4 Developer Agent Competition](https://www.kaggle.com/competitions/gemma-4-developer-agent/overview), with **`graph_find_paths`**, the path-query tool provided by this project. `get_code_neighbors` returns the neighbors of one symbol per call; `graph_find_paths` accepts both endpoints and returns a complete path with evidence for every edge.

The `followup-001` task connects `SpinnerColumn.render` to `DownloadColumn.render` in Rich. Both approaches use the same graph and endpoints. Node IDs below omit the `rich.progress.` prefix for readability.

```mermaid
flowchart LR
    subgraph B["get_code_neighbors: 4 queries + 1 submission"]
        direction TB
        B1["1. get_code_neighbors<br/>node=SpinnerColumn.render<br/>1 neighbor: SpinnerColumn"]
        B2["2. get_code_neighbors<br/>node=SpinnerColumn<br/>6 neighbors, including ProgressColumn"]
        B3["3. get_code_neighbors<br/>node=ProgressColumn<br/>15 neighbors, including DownloadColumn"]
        B4["4. get_code_neighbors<br/>node=DownloadColumn<br/>6 neighbors, including DownloadColumn.render"]
        B5["5. submit_graph_answer<br/>Submit the complete 4-hop path<br/>Accepted"]
        B1 --> B2 --> B3 --> B4 --> B5
    end

    subgraph D["graph_find_paths: 1 query + 1 submission"]
        direction TB
        D1["1. graph_find_paths<br/>source_nodes=[SpinnerColumn.render]<br/>target_nodes=[DownloadColumn.render]<br/>max_hops=4"]
        DR["Traversal within the same call<br/>Complete 4-hop path and evidence for every edge<br/>14 nodes visited; 32 edge rows examined"]
        D2["2. submit_graph_answer<br/>Submit the complete 4-hop path<br/>Accepted"]
        D1 --> DR --> D2
    end
```

In this Rich example, `graph_find_paths` reduces agent query calls from four to one compared with `get_code_neighbors`: **75% fewer query calls**, with the complete 4-hop path and its edge evidence returned together. The [Path-query replay fixture](graph-find-paths/fixtures/real-graph-replays.json) records the path, 14 visited nodes and 32 examined edge rows. The benchmark uses `submit_graph_answer` to submit the result; standalone agents receive the path directly through `graph_find_paths`.

### Run the competition backend

**Only** this optional backend requires authorized Kaggle access and the official SDK. Install it with the existing script:

```bash
bash scripts/setup_runtime.sh
.venv-runtime/bin/code-graph-find-paths \
  --backend competition --graph /absolute/path/original-graph.json \
  --sha256 '<trusted-original-sha256>' \
  query --source rich.progress.SpinnerColumn.render \
        --target rich.progress.DownloadColumn.render
```

The Python equivalent is `CompetitionRuntime(graph_path, expected_graph_sha256)`. This explicitly selects the existing official SDK context, budget gate and owned subprocess workspace. For old MCP configurations that require the SDK backend, add `--backend competition` before `serve`; version 0.2 defaults to standalone. Use the installation commands above to register the executable tool alongside the skill instructions.

For historical replay and real SDK checks (from the repository root):

```bash
python3 scripts/fetch_graphs.py
python3 -I -B graph-find-paths/scripts/verify.py --graphs-dir data/graphs
.venv-runtime/bin/python -I -B scripts/runtime_smoke.py
.venv-runtime/bin/python -m unittest discover -s tests -p 'test_runtime.py' -v
.venv-runtime/bin/python scripts/mcp_smoke.py
```

The [Actions template](.github/verify-workflow.yml.example) is provided for automated checks. It remains a template because the publishing credential lacks workflow-write permission.

## Fixed path-question datasets

The repository contains **50 fixed path questions** in two separate cohorts. They preserve their original IDs and order and serve different evaluation purposes:

| Cohort | Questions / original tasks | Shortest distances | Materials | Historical local experiment |
|---|---:|---|---|---|
| `original32` | 32 / 32 | 8 each at 1, 2, 3 and 4 hops | [Questions, asset mappings, private reference answers and frozen configuration](datasets/original32/) | B/C/D/E/F × 2 repeats = 320 runs |
| `followup18` | 18 / 18 | 16 at 4 hops, 1 at 1 hop, 1 at 2 hops | [Existing historical D tool-output replay fixtures](graph-find-paths/fixtures/real-graph-replays.json) | B/D × 2 repeats = 72 runs |

The 32 questions cover Rich (15), FastAPI (15) and Requests (2). Together the cohorts contain **50 distinct tasks and 50 distinct unordered endpoint pairs**. Family and stratum metadata records related code snapshots for grouped analysis. The table separates question counts from repeated experiment runs. Public `max_hops` is always **4**, including questions with shorter reference paths.

From the repository root, download/verify the two cohorts' original graph assets and validate the combined dataset:

```bash
python3 datasets/original32/fetch_graphs.py --dest data/graphs
python3 scripts/fetch_graphs.py
python3 -I -B datasets/original32/verify.py \
  --graphs-dir data/graphs \
  --followup-fixtures graph-find-paths/fixtures/real-graph-replays.json
```

Use an existing original-graph directory with `--graphs-dir` to avoid downloading. To validate the 32 original assets alone, omit `--followup-fixtures`. The verifier checks graph identity and reports missing, ambiguous or hash-mismatched files.

This check validates **data, asset mappings, shortest distances, reference paths and cohort disjointness**. It runs locally with zero tool calls and zero model requests. The 18 historical replay fixtures have their own verifier. Results are recorded in [the combined dataset verification report](evidence/original32-verification.json).

`questions.jsonl` is host input: pass only `query_id`, `start`, `target`, `max_hops` to a solver. Asset filenames/hashes are host configuration. Keep `answers.private.jsonl`, `provenance.private.jsonl`, `family-strata.jsonl`, reference paths and historical `expected` tool outputs evaluator-only. For `followup18` model input, project only `query_id` and `arguments.source_nodes[0]`, `arguments.target_nodes[0]`, `arguments.max_hops`; never pass `expected`.

Reference paths establish solvability and shortest distance. For model evaluation, accept any valid path whose edges were delivered by a tool during that attempt; use the recorded tool trace to check this condition. Use the supplied dataset validator to check graph assets and reference paths.

This package provides the D backend and the datasets for reproducing path-finding experiments. Historical `original32` used Chat Completions; the complete `followup18` batch used stateless asynchronous Responses. Record the model, service and protocol for each run, and report results by cohort for comparable measurements.

## What a receiving agent should verify

1. The host exposes exactly `graph_find_paths`, with the frozen schema in [`tool-schema.json`](graph-find-paths/references/tool-schema.json).
2. A valid query consumes one native tool call, performs at most one core search, and delivers the entire result unchanged.
3. Invalid/unknown calls are counted by its outer controller, exhausted budgets are rejected, and shutdown closes the owned session/workspace.
4. For competition reproduction its graph SHA matches the task asset; for real SWE work, separately check source snapshot/base-commit alignment.
5. If testing model performance, use the frozen benchmark system/schema and keep all acceptance fixtures out of solver context.

## Validation results

[Standalone verification](evidence/standalone-summary.json) records a clean installed wheel running Python/CLI without third-party runtime dependencies and MCP with only its optional transport dependencies, with no Kaggle or competition SDK installed. Fourteen standalone tests passed; the optional SDK retained all five integration tests, and the unchanged frozen verifier passed 41 tests plus 18 original-graph replays. The standalone engine also matched all 18 reference packets after excluding dynamic timing/length and its deliberately different host-audit label.

[`evidence/verification.json`](evidence/verification.json) records the 41 tests and 18 replays. [`evidence/runtime-summary.json`](evidence/runtime-summary.json) records the actual official `SwegemmaContext`, `SubprocessManager` and `ToolRegistry` check: exact name/schema, one-call budget increment, full edge-row evidence, budget/session rejection and workspace cleanup. In that optional backend, the SDK budget module matches the frozen SHA256. [MCP evidence](evidence/mcp-summary.json) verifies real client discovery, calls, attempt limits and disconnect cleanup. [Package evidence](evidence/package-summary.json) verifies the built wheel works outside the checkout. The source-provenance record identifies original implementation hashes and documents the equivalent Unicode-escape normalization in a test file.

For competition integration, register the tool in a host that supports custom tools. Kaggle scoring uses its own tool registry and declarative submission format.

## Layout

- [`graph-find-paths/`](graph-find-paths/): hash-pinned implementation, schema, skill and historical fixtures.
- [`datasets/original32/`](datasets/original32/): byte-pinned original 32 questions, asset mappings, evaluator-only reference answers and data validation scripts.
- [`code_graph_find_paths/`](code_graph_find_paths/): installable host adapter, CLI and MCP transport.
- [`scripts/`](scripts/): setup, data download and runtime smoke.
- [`evidence/`](evidence/): portable verification summaries; raw local paths/logs are excluded.

Graph files, SDK wheels, local environments and raw runtime records are excluded from Git. Internal transfer documents and attachment-recovery utilities are excluded from the distribution. Source provenance is recorded with the implementation; official SDK wheels are obtained from their distributor.

Data: [Kaggle competition](https://www.kaggle.com/competitions/gemma-4-developer-agent/data). SDK: [official wheelhouse](https://www.kaggle.com/datasets/metric/gemma-4-developer-agent-wheelhouse).
