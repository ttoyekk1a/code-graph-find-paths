import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tool import AllPathsSession, make_graph_find_paths


class AllPathsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def session(self, edge_pairs, ids="abcdz", padding=0, **limits):
        rows = [
            {"source": a, "target": b, "key": i, "extra": "é" * padding}
            for i, (a, b) in enumerate(edge_pairs)
        ]
        path = Path(self.directory.name) / "graph.json"
        raw = json.dumps({"nodes": [{"id": node} for node in ids], "links": rows}).encode()
        path.write_bytes(raw)
        return AllPathsSession(path, hashlib.sha256(raw).hexdigest(), **limits)

    def sequences(self, result, pair=0):
        nodes, edges = result["nodes"], result["edges"]
        self.assertEqual(len(nodes), len(set(nodes)))
        used_nodes, used_edges = set(), set()
        for record in result["pairs"]:
            for path in record["paths"]:
                used_nodes.update(path["nodes"])
                used_edges.update(path["edges"])
                self.assertEqual(len(path["nodes"]), len(set(path["nodes"])))
                self.assertEqual(len(path["edges"]), len(path["nodes"]) - 1)
                for a, b, edge in zip(path["nodes"], path["nodes"][1:], path["edges"]):
                    self.assertEqual({a, b}, set(edges[edge]["nodes"]))
        self.assertEqual(used_nodes, set(range(len(nodes))))
        self.assertEqual(used_edges, set(range(len(edges))))
        return [tuple(nodes[i] for i in path["nodes"]) for path in result["pairs"][pair]["paths"]]

    def test_diamond_longer_alternatives_cycles_and_raw_evidence(self):
        tool = self.session(["ab", "bd", "ac", "cd", "ad", "bc", "ba", "ab", "aa"])
        result = tool.graph_find_paths(["a"], ["d"])
        self.assertTrue(result["complete"])
        sequences = self.sequences(result)
        edge = next(edge for edge in result["edges"]
                    if {result["nodes"][i] for i in edge["nodes"]} == {"a", "b"})
        self.assertEqual([r["index"] for r in edge["rows"]], [0, 6, 7])
        self.assertEqual(edge["rows"][1]["row"], {"source": "b", "target": "a", "key": 6, "extra": ""})
        self.assertEqual(tool.graph_find_paths(["a"], ["d"]), result)
        self.assertEqual(self.sequences(tool.graph_find_paths(["a"], ["d"], 1)), [("a", "d")])

    def test_four_hops_and_direction(self):
        tool = self.session(["ab", "bc", "cd", "dz"])
        self.assertEqual(self.sequences(tool.graph_find_paths(["z"], ["a"])), [tuple("zdcba")])
        self.assertEqual(tool.graph_find_paths(["z"], ["a"], 3)["pairs"][0]["status"], "no_path")

    def test_identity_disconnect_and_resolution(self):
        tool = self.session([], ids=["pkg.a", "pkg.b", "other.b", "z"])
        result = tool.graph_find_paths(["a", "missing"], ["pkg.a", "z", "b"])
        self.assertEqual(self.sequences(result), [("pkg.a",)])
        self.assertEqual([p["status"] for p in result["pairs"]],
                         ["connected", "no_path", "unresolved", "unresolved", "unresolved", "unresolved"])
        self.assertEqual(result["resolution"]["sources"][0]["status"], "suffix")
        self.assertEqual(result["resolution"]["targets"][2]["status"], "AmbiguousNode")
        self.assertFalse(result["complete"])
        self.assertTrue(result["pairs"][1]["complete"])

    def test_invocation_wide_guards_and_duplicate_pair_reuse(self):
        edges = ["ab", "bd", "ac", "cd"]
        result = self.session(edges, max_paths=2).graph_find_paths(["a", "a"], ["d", "b"])
        self.assertEqual(result["work"]["unique_paths"], 2)
        self.assertEqual(self.sequences(result, 0), self.sequences(result, 2))
        self.assertTrue(result["pairs"][0]["complete"])
        self.assertFalse(result["pairs"][1]["search_complete"])
        self.assertEqual(result["pairs"][1]["status"], "incomplete")
        for key, reason in [("max_edge_rows", "edge_row_limit"), ("max_dfs_states", "dfs_state_limit")]:
            result = self.session(edges, **{key: 1}).graph_find_paths(["a"], ["d", "b"])
            self.assertIn(reason, result["truncation"])
            self.assertFalse(result["search_complete"])
            self.assertTrue(all(p["status"] != "no_path" for p in result["pairs"]))

    def test_exact_output_cap_and_whole_edge_evidence(self):
        edge_pairs = ["ab", "ba", "ac", "cd", "ad"]
        full = self.session(edge_pairs, padding=400).graph_find_paths(["a"], ["d", "b"])
        exact = len(json.dumps(full))
        self.assertGreater(exact, 4096)
        result = self.session(edge_pairs, padding=400, output_limit=exact).graph_find_paths(["a"], ["d", "b"])
        self.assertEqual(result, full)
        partial = self.session(edge_pairs, padding=400, output_limit=exact - 1).graph_find_paths(["a"], ["d", "b"])
        self.assertLessEqual(len(json.dumps(partial)), exact - 1)
        self.assertTrue(partial["search_complete"])
        self.assertFalse(partial["output_complete"])
        self.assertFalse(partial["complete"])
        self.sequences(partial)
        for edge in partial["edges"]:
            original = next(item for item in full["edges"] if item["rows"][0]["index"] == edge["rows"][0]["index"])
            self.assertEqual(edge["rows"], original["rows"])

    def test_large_resolution_fallback_and_invalid_inputs(self):
        tool = self.session([], output_limit=4096)
        result = tool.graph_find_paths(["x" * 5000] * 3, ["y" * 5000] * 8)
        self.assertLessEqual(len(json.dumps(result)), 4096)
        self.assertTrue(all(p["status"] == "unresolved" for p in result["pairs"]))
        self.assertIn("output_limit", result["truncation"])
        for sources, targets, hops in [("a", ["b"], 4), ([], ["b"], 4), (["a"], ["b"], True),
                                      (["a"], ["b"] * 9, 4), (["a"], ["b"], 5)]:
            with self.assertRaises(ValueError):
                tool.graph_find_paths(sources, targets, hops)
        with self.assertRaisesRegex(ValueError, "SHA256Mismatch"):
            make_graph_find_paths(Path(self.directory.name) / "graph.json", "0" * 64)


if __name__ == "__main__":
    unittest.main()
