#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3.12}"
command -v "$PYTHON" >/dev/null || { echo 'Python 3.12+ is required. Set PYTHON=/path/to/python.' >&2; exit 1; }
command -v kaggle >/dev/null || { echo 'Install and authenticate the Kaggle CLI first.' >&2; exit 1; }
mkdir -p runtime-deps evidence
for wheel in swegemma-0.2.11-py3-none-any.whl adk_eval_core-0.1.0-py3-none-any.whl adk_submission-0.2.13-py3-none-any.whl; do
  if [ ! -f "runtime-deps/$wheel" ]; then
    kaggle datasets download -d metric/gemma-4-developer-agent-wheelhouse -f "$wheel" -p runtime-deps -q
  fi
done
# Refuse a different SDK budget gate before installing or executing any SDK code.
"$PYTHON" - <<'PY'
from pathlib import Path
import hashlib, zipfile
p=Path('runtime-deps/swegemma-0.2.11-py3-none-any.whl')
with zipfile.ZipFile(p) as z:
    actual=hashlib.sha256(z.read('swegemma/tools/base.py')).hexdigest()
expected='535a0e6d121549b41906685d0e440e8709c4b8b434bc31dcc82fa7740ec66281'
if actual != expected: raise SystemExit('BudgetGateVersionMismatch: obtain the matching official wheel; do not bypass this check.')
PY
"$PYTHON" -m venv .venv-runtime
.venv-runtime/bin/python -m pip install runtime-deps/adk_eval_core-0.1.0-py3-none-any.whl runtime-deps/adk_submission-0.2.13-py3-none-any.whl 'google-adk==1.36.1' 'fastapi==0.124.1' networkx pandas litellm python-dotenv
.venv-runtime/bin/python -m pip install --no-deps runtime-deps/swegemma-0.2.11-py3-none-any.whl
.venv-runtime/bin/python -m pip install -e ".[mcp]"
printf '\nInstalled. Run: %s/.venv-runtime/bin/code-graph-find-paths --help\n' "$ROOT"
