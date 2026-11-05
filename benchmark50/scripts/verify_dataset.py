"""Verify all raw graph hashes and re-enumerate gold paths with the independent oracle."""
import argparse
import json
from benchmark50.dataset import verify_dataset

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--graph-dir',required=True)
    args=p.parse_args(argv);print(json.dumps(verify_dataset(args.graph_dir),indent=2))
if __name__=='__main__':main()
