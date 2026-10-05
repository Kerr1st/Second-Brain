#!/usr/bin/env python3
"""Plan source-identity deduplication. Applying requires the exact reviewed hash."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from src.db.maintenance import plan_dedup,apply_dedup,write_manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='dedup-manifest.json')
    parser.add_argument('--apply-manifest',type=Path)
    parser.add_argument('--confirm-sha256')
    args=parser.parse_args(argv)
    if args.apply_manifest:
        if not args.confirm_sha256: parser.error('--apply-manifest requires --confirm-sha256')
        count=apply_dedup(json.loads(args.apply_manifest.read_text()),args.confirm_sha256)
        print(json.dumps({'applied':count}));return 0
    plan=plan_dedup();sha=write_manifest(plan,args.output)
    print(json.dumps({'dry_run':True,'operations':len(plan['operations']),'sha256':sha,'manifest':args.output}))
    return 0


if __name__=='__main__':raise SystemExit(main())
