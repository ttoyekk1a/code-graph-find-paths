"""JSON-only CLI output and optional MCP stdio serving."""
import argparse
import json
import sys
from .api import GraphRuntime, decode_paths


def main(argv=None):
    parser = argparse.ArgumentParser(prog='code-graph-find-paths')
    parser.add_argument('--graph', required=True)
    parser.add_argument('--sha256', help='Expected graph hash; omitted means pin the supplied bytes')
    parser.add_argument('--max-edge-rows', type=int, default=2_000_000)
    parser.add_argument('--max-dfs-states', type=int, default=500_000)
    parser.add_argument('--max-paths', type=int, default=100_000)
    parser.add_argument('--output-limit', type=int, default=1_000_000)
    sub = parser.add_subparsers(dest='command', required=True)
    query = sub.add_parser('query')
    query.add_argument('--source', action='append', required=True)
    query.add_argument('--target', action='append', required=True)
    query.add_argument('--max-hops', type=int, default=4)
    query.add_argument('--decode', action='store_true', help='Emit decoded node sequences instead of the packet')
    sub.add_parser('serve', help='Run the optional MCP stdio transport')
    args = parser.parse_args(argv)
    try:
        with GraphRuntime(args.graph, args.sha256, max_edge_rows=args.max_edge_rows,
                          max_dfs_states=args.max_dfs_states, max_paths=args.max_paths,
                          output_limit=args.output_limit) as runtime:
            if args.command == 'query':
                packet = runtime.query(args.source, args.target, args.max_hops)
                print(json.dumps(decode_paths(packet) if args.decode else packet))
            else:
                try:
                    import anyio
                    from .mcp_server import serve
                except ModuleNotFoundError as exc:
                    raise RuntimeError("Install MCP support from the project: python -m pip install '.[mcp]'") from exc
                anyio.run(serve, runtime)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
