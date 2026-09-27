#!/usr/bin/env python3
"""Dry-run and resumable chunk repair. Dry run never calls an embedding provider."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from src.db.maintenance import plan_rechunk,apply_rechunk,write_manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strip-known-ide-context',action='store_true',help='Plan structural context removal in replacement chunks; retain original parent')
    parser.add_argument('--output',default='rechunk-manifest.json')
    parser.add_argument('--apply-manifest',type=Path)
    parser.add_argument('--confirm-sha256')
    parser.add_argument('--allow-paid-embedding',action='store_true')
    parser.add_argument('--cache-dir',type=Path,default=Path('.maintenance/embeddings'))
    args=parser.parse_args(argv)
    if args.apply_manifest:
        if not args.confirm_sha256 or not args.allow_paid_embedding:
            parser.error('Application requires --confirm-sha256 and --allow-paid-embedding after pricing and authorization')
        from src.embeddings import generate_embedding, EMBEDDING_MODEL
        plan=json.loads(args.apply_manifest.read_text())
        if plan['embedding_model'] != EMBEDDING_MODEL:
            parser.error('Embedding model differs from reviewed manifest')
        count=apply_rechunk(plan,args.confirm_sha256,embed=generate_embedding,cache_dir=args.cache_dir)
        print(json.dumps({'applied':count}));return 0
    plan=plan_rechunk(strip_known_ide_context=args.strip_known_ide_context);sha=write_manifest(plan,args.output)
    print(json.dumps({'dry_run':True,'provider_calls':0,'planned_embedding_calls':plan['embedding_calls'],
                      'input_characters':plan['input_characters'],'sha256':sha,'manifest':args.output}))
    return 0


if __name__=='__main__':raise SystemExit(main())
