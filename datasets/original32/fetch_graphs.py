"""Explicit original32 graph download using the recipient's authorized Kaggle CLI."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile
ROOT=Path(__file__).resolve().parent

def main():
    p=argparse.ArgumentParser();p.add_argument('--dest',type=Path,required=True);args=p.parse_args()
    manifest=json.loads((ROOT/'MANIFEST.json').read_text());raw=(ROOT/'assets.jsonl').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['files']['assets.jsonl']:raise ValueError('AssetsHashMismatch')
    args.dest.mkdir(parents=True,exist_ok=True)
    for row in map(json.loads,raw.decode().splitlines()):
        name=row['graph_filename'];expected=row['graph_sha256']
        if Path(name).name!=name:raise ValueError('UnsafeFilename')
        dst=args.dest/name
        if dst.exists():
            if hashlib.sha256(dst.read_bytes()).hexdigest()!=expected:raise ValueError('ExistingGraphHashMismatch:'+name)
            print('Already verified:',name);continue
        with tempfile.TemporaryDirectory(prefix='original32-download-') as td:
            subprocess.run(['kaggle','competitions','download','gemma-4-developer-agent','-f','graphs/'+name,'-p',td,'-q'],check=True)
            found=list(Path(td).rglob(name))
            if len(found)==1:data=found[0].read_bytes()
            elif found:raise ValueError('AmbiguousDownload:'+name)
            else:
                archives=list(Path(td).rglob('*.zip'))
                if len(archives)!=1:raise ValueError('MissingGraph:'+name)
                with zipfile.ZipFile(archives[0]) as z:
                    entries=[n for n in z.namelist() if Path(n).name==name]
                    if len(entries)!=1:raise ValueError('AmbiguousArchive:'+name)
                    data=z.read(entries[0])
            if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('DownloadedGraphHashMismatch:'+name)
            with dst.open('xb') as stream:stream.write(data)
        print('Downloaded and verified:',name)
if __name__=='__main__':main()
