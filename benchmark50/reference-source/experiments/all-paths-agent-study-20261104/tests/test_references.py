from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import run_references


class ReferenceTests(unittest.TestCase):
    def test_32_limit_and_full_names(self):
        self.assertEqual(run_references.reference_config()['max_graph_queries'],32)
        self.assertEqual(run_references.LABELS,{'legacy-program':'旧工具＋确定性搜索程序','all-paths-direct':'全路径工具直接执行'})


if __name__=='__main__':unittest.main()
