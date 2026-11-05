"""Summarize all outcomes, keeping failures and unknown token usage visible."""
import argparse
import csv
import json
from pathlib import Path
from benchmark50.support import CONFIG,HERE,save

LABELS={'legacy-tools-agent':'Competition tools','guided-legacy-tools-agent':'Competition tools + search guidance','all-paths-tool-agent':'graph_find_paths'}

def summarize(rows):
    result={}
    seen=set()
    for row in rows:
        key=(row['query_id'],row['arm'])
        if key in seen:raise ValueError('Duplicate episode result')
        seen.add(key)
    for arm,label in LABELS.items():
        group=[r for r in rows if r['arm']==arm];n=len(group)
        if n>50:raise ValueError('More than 50 outcomes in an arm')
        exact=sum(bool(r.get('success')) for r in group)
        unknown=sum(r['usage']['unknown_usage_responses'] for r in group)
        prompt=sum(r['usage']['prompt_tokens'] for r in group)
        completion=sum(r['usage']['completion_tokens'] for r in group)
        result[label]=dict(started=n,planned=50,missing=50-n,status='complete' if n==50 else 'partial',
            exact_success=exact,success_rate=exact/50,
            mean_path_precision=sum(r['grade']['precision'] for r in group)/n if n else None,
            mean_path_recall=sum(r['grade']['recall'] for r in group)/n if n else None,
            mean_graph_attempts=sum(r['counters']['graph_attempts'] for r in group)/n if n else None,
            mean_python_attempts=sum(r['counters']['python_attempts'] for r in group)/n if n else None,
            mean_model_rounds=sum(r['counters']['model_rounds'] for r in group)/n if n else None,
            mean_denominator=n,certified_success=sum(r['grade']['certified_success'] and r['answer'] is not None for r in group),
            false_complete_claims=sum(r['grade']['false_complete_claim'] for r in group),
            unverified_complete_claims=sum(r['grade']['unverified_complete_claim'] for r in group),
            prompt_tokens_known=prompt,completion_tokens_known=completion,total_tokens_known=prompt+completion,
            mean_tokens_known=(prompt+completion)/n if n else None,
            total_tokens=prompt+completion if not unknown else None,
            mean_tokens=(prompt+completion)/n if n and not unknown else None,
            unknown_usage_responses=unknown,token_usage_complete=unknown==0 and n==50)
    return result

def aggregate_run(out):
    from benchmark50.scripts.evaluate import evaluate_run
    evaluate_run(out);out=Path(out)
    rows=[json.loads(p.read_text()) for p in sorted(out.glob('*/evaluated.json'))]
    summary=summarize(rows)
    mode=json.loads((out/'manifest.json').read_text())['mode']
    for group in summary.values():
        group['execution_kind']=mode
        if mode=='mock':group['real_model_requests']=0
    save(out/'summary.json',summary)
    questions=[json.loads(line) for line in (HERE/'data/questions.jsonl').read_text().splitlines()]
    by_id={(r['query_id'],r['arm']):r for r in rows}
    fields=['query_id','configuration','status','success','stop_reason','error_type','graph_attempts','python_attempts','model_rounds','tokens_known','usage_unknown']
    with (out/'tasks.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for q in questions:
            for arm,label in LABELS.items():
                row=by_id.get((q['query_id'],arm))
                record=dict(query_id=q['query_id'],configuration=label,status='missing' if row is None else 'finished')
                if row:
                    record.update(success=row['success'],stop_reason=row['stop_reason'],error_type=(row.get('error') or {}).get('type',''),
                        **{k:row['counters'][k] for k in ('graph_attempts','python_attempts','model_rounds')},
                        tokens_known=row['usage']['prompt_tokens']+row['usage']['completion_tokens'],usage_unknown=bool(row['usage']['unknown_usage_responses']))
                writer.writerow(record)
    return summary

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',required=True)
    print(json.dumps(aggregate_run(p.parse_args(argv).run),indent=2))
if __name__=='__main__':main()
