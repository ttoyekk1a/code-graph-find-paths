"""Command-line entry point. Protocol/result JSON is the only stdout output."""
import argparse
import contextlib
import json
import logging
from pathlib import Path
import sys


def parser():
    root = argparse.ArgumentParser(prog='code-graph-find-paths')
    root.add_argument('--graph', type=Path, required=True, help='Original graph JSON (never rewritten)')
    root.add_argument('--sha256', help='Optional expected graph SHA256; required for competition backend')
    root.add_argument('--backend', choices=['standalone', 'competition'], default='standalone')
    root.add_argument('--tool-calls', type=int, default=8, help='Total tool attempt budget (default: 8)')
    commands = root.add_subparsers(dest='command', required=True)
    query = commands.add_parser('query', help='Run one query and print the complete JSON result')
    query.add_argument('--source', action='append', required=True, help='Source ID; repeat for multiple sources')
    query.add_argument('--target', action='append', required=True, help='Target ID; repeat for multiple targets')
    query.add_argument('--max-hops', type=int, default=4)
    serve = commands.add_parser('serve', help='Serve the single graph_find_paths MCP tool')
    serve.add_argument('transport', choices=['stdio'], nargs='?', default='stdio')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        # SDK initialization may print; keep stdout reserved for the result/protocol.
        with contextlib.redirect_stdout(sys.stderr):
            from .runtime import GraphRuntime, CompetitionRuntime
        if args.backend == 'competition' and not args.sha256:
            raise ValueError('--sha256 is required for --backend competition')
        runtime_class = CompetitionRuntime if args.backend == 'competition' else GraphRuntime
        runtime = runtime_class(args.graph, args.sha256, tool_calls=args.tool_calls)
        with contextlib.redirect_stdout(sys.stderr):
            runtime.__enter__()
        try:
            if args.command == 'query':
                with contextlib.redirect_stdout(sys.stderr):
                    result = runtime.query(source_nodes=args.source, target_nodes=args.target,
                                           max_hops=args.max_hops)
                print(json.dumps(result))
                return 1 if result.get('status') == 'error' else 0
            try:
                from .mcp_server import serve_stdio
            except ModuleNotFoundError as exc:
                if exc.name == 'mcp':
                    raise RuntimeError("Install MCP support from the checkout: python -m pip install '.[mcp]'") from exc
                raise
            import anyio
            anyio.run(serve_stdio, runtime)
            return 0
        finally:
            with contextlib.redirect_stdout(sys.stderr):
                runtime.close()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
