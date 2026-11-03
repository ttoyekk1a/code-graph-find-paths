"""Ship the retained, hash-pinned reference bundle inside built wheels."""
from pathlib import Path
import shutil
from setuptools import setup
from setuptools.command.build_py import build_py

class BuildWithFrozenBundle(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / 'graph-find-paths'
        target = Path(self.build_lib) / 'code_graph_find_paths' / '_frozen_bundle'
        # Prevent removed source files from surviving in an incremental build.
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

setup(cmdclass={'build_py': BuildWithFrozenBundle})
