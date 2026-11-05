"""Verify frozen inputs and the portable implementation before execution."""
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()

def original_path(name):
    return ROOT / (name[len('benchmark50/'):] if name.startswith('benchmark50/') else name)

def verify_sources():
    original = json.loads((ROOT/'HANDOFF_MANIFEST.json').read_text())
    for name, record in original['files'].items():
        path = original_path(name)
        if path.stat().st_size != record['bytes'] or digest(path) != record['sha256']:
            raise ValueError('Frozen source mismatch: '+name)
    manifest = json.loads((ROOT/'MANIFEST.json').read_text())
    for name, expected in manifest['files'].items():
        if digest(ROOT/name) != expected:
            raise ValueError('Portable source mismatch: '+name)
    return manifest['files']
