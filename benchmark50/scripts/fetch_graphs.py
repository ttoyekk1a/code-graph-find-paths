"""Fetch exact competition files using the recipient's Kaggle CLI configuration."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
from benchmark50.integrity import ROOT,digest,verify_sources

def fetch(directory):
    verify_sources();directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    assets=json.loads((ROOT/'data/graph-assets.json').read_text())
    fetched=0
    for asset in assets:
        dest=directory/asset['graph_filename']
        if dest.exists():
            if dest.stat().st_size != asset['bytes'] or digest(dest)!=asset['sha256']:
                raise ValueError('Existing graph differs; refusing to replace: '+dest.name)
            continue
        with tempfile.TemporaryDirectory(prefix='graph-download-') as tmp:
            subprocess.run(['kaggle','competitions','download',asset['competition'],'-f',asset['competition_file'],'-p',tmp,'-q'],check=True)
            candidates=list(Path(tmp).rglob(dest.name))
            if not candidates:
                for archive in Path(tmp).glob('*.zip'):
                    with zipfile.ZipFile(archive) as z:
                        matches=[n for n in z.namelist() if Path(n).name==dest.name]
                        if len(matches)==1:
                            extracted=Path(tmp)/dest.name;extracted.write_bytes(z.read(matches[0]));candidates.append(extracted)
            if len(candidates)!=1:raise ValueError('Expected one downloaded graph: '+dest.name)
            path=candidates[0]
            if path.stat().st_size!=asset['bytes'] or digest(path)!=asset['sha256']:raise ValueError('Downloaded graph mismatch: '+dest.name)
            stage=directory/(dest.name+'.partial');shutil.copyfile(path,stage);stage.replace(dest);fetched+=1
    return dict(graphs=50,downloaded=fetched,verified=50)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--graph-dir',required=True)
    print(json.dumps(fetch(p.parse_args(argv).graph_dir),indent=2))
if __name__=='__main__':main()
