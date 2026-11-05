"""Compare historical and product packets on all 50 graphs at the frozen hop bound."""
import argparse
import json
from benchmark50.dataset import load_inputs
from benchmark50.support import make_backend

def compare(graph_dir):
    questions,refs=load_inputs(graph_dir)
    for q in questions:
        ref=refs[q['query_id']]
        old=make_backend('all-paths-tool-agent',ref)
        new=make_backend('all-paths-tool-agent',ref,'product')
        args=dict(source_nodes=[q['start']],target_nodes=[q['target']])
        historical=old.query('graph_find_paths',args)
        default=new.query('graph_find_paths',args)
        explicit=new.query('graph_find_paths',dict(args,max_hops=4))
        if default!=explicit:raise ValueError('Default/explicit bound mismatch: '+q['query_id'])
        if {k:v for k,v in historical.items() if k!='version'} != {k:v for k,v in default.items() if k!='version'}:
            raise ValueError('Historical/product packet mismatch: '+q['query_id'])
    return dict(questions=50,default_and_explicit_four_equal=True,all_fields_except_version_equal=True,
                original_edge_evidence_equal=True,model_requests=0)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--graph-dir',required=True)
    print(json.dumps(compare(p.parse_args(argv).graph_dir),indent=2))
if __name__=='__main__':main()
