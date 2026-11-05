"""Keep the authoritative tool and frozen loader at their required relative paths."""
from pathlib import Path
import json
import shutil
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildBundle(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        target = Path(self.build_lib) / 'code_graph_find_paths/_bundle'
        if target.exists():
            shutil.rmtree(target)
        names = list(json.loads((root / 'SHA256SUMS.json').read_text())) + ['SHA256SUMS.json', 'verify_sources.py']
        for name in names:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, dest)

        benchmark = root / 'benchmark50'
        target = Path(self.build_lib) / 'benchmark50'
        for name in list(json.loads((benchmark / 'MANIFEST.json').read_text())['files']) + ['MANIFEST.json']:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(benchmark / name, dest)


setup(cmdclass={'build_py': BuildBundle})
