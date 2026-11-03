"""Fetch only pinned original graphs; never regenerate or rewrite graph JSON."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib, json, shutil, subprocess, tempfile, zipfile
ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'data/graphs'

def fetch(row):
    name, sha = row['graph_filename'], row['graph_sha256']
    target = DEST / name
    if not target.exists():
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(['kaggle', 'competitions', 'download', 'gemma-4-developer-agent', '-f', 'graphs/' + name, '-p', tmp, '-q'], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(name + ': ' + result.stderr + result.stdout)
            candidates = list(Path(tmp).rglob(name))
            if candidates:
                data = candidates[0].read_bytes()
            else:
                archives = list(Path(tmp).rglob('*.zip'))
                if len(archives) != 1: raise RuntimeError('Missing download: ' + name)
                with zipfile.ZipFile(archives[0]) as z:
                    matches = [n for n in z.namelist() if Path(n).name == name]
                    if len(matches) != 1: raise RuntimeError('Ambiguous archive: ' + name)
                    data = z.read(matches[0])
            if hashlib.sha256(data).hexdigest() != sha: raise RuntimeError('Graph hash mismatch: ' + name)
            with target.open('xb') as f: f.write(data)
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual != sha: raise RuntimeError('Graph hash mismatch: ' + name)
    print('Verified ' + name, flush=True)
    return {'file':name, 'sha256':actual, 'bytes':target.stat().st_size}

if __name__ == '__main__':
    DEST.mkdir(parents=True, exist_ok=True)
    rows = json.loads((ROOT/'graph-find-paths/fixtures/real-graph-replays.json').read_text())
    with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(fetch, rows))
    (ROOT/'evidence/graph-assets.json').write_text(json.dumps(results, indent=2)+'\n')
