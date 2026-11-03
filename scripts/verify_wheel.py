"""Check that an installable wheel carries the complete byte-pinned bundle."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

wheel = Path(sys.argv[1])
prefix = 'code_graph_find_paths/_frozen_bundle/'
with zipfile.ZipFile(wheel) as archive:
    manifest = json.loads(archive.read(prefix + 'MANIFEST.json'))
    expected_names = {prefix + name for name in manifest['files']} | {prefix + 'MANIFEST.json'}
    actual_names = {name for name in archive.namelist() if name.startswith(prefix) and not name.endswith('/')}
    if actual_names != expected_names:
        raise SystemExit('Wheel bundle contains missing or unexpected files')
    for relative, expected in manifest['files'].items():
        actual = hashlib.sha256(archive.read(prefix + relative)).hexdigest()
        if actual != expected:
            raise SystemExit('Wheel source mismatch: ' + relative)
    entries = archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/entry_points.txt'))).decode()
    assert 'code-graph-find-paths = code_graph_find_paths.cli:main' in entries
print(f"Wheel entry point and all {len(manifest['files'])} manifest-pinned payload files verified; no extra bundle files.")
