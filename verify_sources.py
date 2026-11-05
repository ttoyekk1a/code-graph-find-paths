"""Check every executable/input source against the project's source hashes."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
hashes = json.loads((root / "SHA256SUMS.json").read_text())
for relative, expected in hashes.items():
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit("SHA256 mismatch: " + relative)
    print("OK " + relative + " " + actual)
