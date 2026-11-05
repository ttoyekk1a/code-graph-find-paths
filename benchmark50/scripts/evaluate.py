"""Grade saved terminal submissions using delivered evidence, without model calls."""
import argparse
import json
from pathlib import Path
from benchmark50.integrity import ROOT,verify_sources
from benchmark50.scripts.run import collect_rows

def evaluate_run(out):
    verify_sources();out=Path(out)
    manifest=json.loads((out/'manifest.json').read_text())
    if manifest.get('mode') not in {'model','mock'}:raise ValueError('Use the deterministic run summary for offline controls')
    refs={r['query_id']:r for r in json.loads((ROOT/'evaluator/reference-answers.json').read_text())}
    rows=collect_rows(out,refs)
    return dict(execution_kind=manifest['mode'],evaluated=len(rows),planned=150,status='complete' if len(rows)==150 else 'partial',model_requests=0)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',required=True)
    print(json.dumps(evaluate_run(p.parse_args(argv).run),indent=2))
if __name__=='__main__':main()
