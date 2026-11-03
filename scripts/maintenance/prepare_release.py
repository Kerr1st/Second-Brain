#!/usr/bin/env python3
"""Prepare read-only release manifests; metadata application is an explicit separate mode."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from src.db.maintenance import plan_metadata_repairs,plan_role_audit,apply_metadata_repairs,write_manifest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default='release-metadata-manifest.json')
    p.add_argument('--roles-and-indexes',action='store_true',help='read-only audit of the 017 role model and zero-scan indexes')
    p.add_argument('--apply-manifest',type=Path)
    p.add_argument('--confirm-sha256')
    a=p.parse_args(argv)
    if a.apply_manifest:
        if not a.confirm_sha256:p.error('Application requires the reviewed --confirm-sha256')
        print(json.dumps({'applied':apply_metadata_repairs(json.loads(a.apply_manifest.read_text()),a.confirm_sha256)}));return 0
    plan=plan_role_audit() if a.roles_and_indexes else plan_metadata_repairs()
    sha=write_manifest(plan,a.output)
    print(json.dumps({'dry_run':True,'kind':plan['kind'],'operations':len(plan.get('operations',[])),'sha256':sha,'manifest':a.output}))
    return 0


if __name__=='__main__':raise SystemExit(main())
