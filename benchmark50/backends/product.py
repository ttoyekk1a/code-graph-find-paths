"""Explicit product substitution; experimental schemas remain frozen at four hops."""
from code_graph_find_paths import GraphRuntime

class ProductCandidate:
    def __init__(self, graph_path, expected_sha256, config):
        self.runtime = GraphRuntime(graph_path, expected_sha256,
            **{k: config['candidate'][k] for k in ('max_edge_rows','max_dfs_states','max_paths')},
            output_limit=config['max_result_chars'])
    def query(self, name, args):
        if name != 'graph_find_paths': raise ValueError('Unknown candidate tool')
        return self.runtime.graph_find_paths(**args)
