"""Run the exact all-paths callable with a user-supplied graph and pinned SHA."""
import argparse
import importlib.util
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--sources", nargs="+", required=True)
    parser.add_argument("--targets", nargs="+", required=True)
    parser.add_argument("--max-hops", type=int, default=4)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "all_paths_tool", root / "variants/graph-find-paths/tool.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    graph_find_paths = module.make_graph_find_paths(args.graph, args.sha256)
    packet = graph_find_paths(args.sources, args.targets, args.max_hops)
    print(json.dumps(packet, indent=2))
    # Every index is local to this packet. In minimal mode paths can be omitted.
    decoded = [
        [[packet["nodes"][index] for index in path["nodes"]]
         for path in pair.get("paths", [])]
        for pair in packet["pairs"]
    ]
    print("DECODED_PAIRS=" + json.dumps(decoded))


if __name__ == "__main__":
    main()
