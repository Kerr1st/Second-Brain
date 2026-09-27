#!/usr/bin/env python3
"""Rehearse migrations and unpaid corpus repair on an encrypted backup clone.

Creates its own socket-only cluster. No existing database target is accepted.
Dedup and metadata repairs affect only that disposable restore. Rechunking is
planned, never embedded. Output manifests cannot be applied to the live database.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.maintenance import backup_bundle as backup
from scripts.eval.discovery_trial import run_process,TrialInterrupted

DB_NAME='sb_release_qualification'
DB_USER='sb_qualify'


def private_json(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2,default=str);stream.write('\n')


def isolated_environment(area,pg_bin):
    """No credentials or inherited database settings enter the repair process."""
    area=Path(area)
    return {'PATH':f'{pg_bin}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        'HOME':str(area/'home'),'PYTHONPATH':str(ROOT),'LANG':'en_US.UTF-8',
        'PYTHONDONTWRITEBYTECODE':'1','SECOND_BRAIN_PROFILE':'controlled',
        'DB_HOST':str(area/'socket'),'DB_PORT':'5432','DB_NAME':DB_NAME,'DB_USER':DB_USER,'DB_PASSWORD':'',
        'PGHOST':str(area/'socket'),'PGPORT':'5432','PGDATABASE':DB_NAME,'PGUSER':DB_USER,
        'PGPASSFILE':str(area/'absent-pgpass'),'AWS_EC2_METADATA_DISABLED':'true',
        'AWS_CONFIG_FILE':str(area/'absent-aws-config'),'AWS_SHARED_CREDENTIALS_FILE':str(area/'absent-aws-credentials'),
        'EMBEDDING_MODEL':'amazon.titan-embed-text-v2:0'}


def assert_target(marker):
    marker=Path(marker).resolve();area=marker.parent;value=json.loads(marker.read_text())
    if area.parent!=Path('/private/tmp') or not area.name.startswith('sbqual-') or area.stat().st_mode&0o077:
        raise ValueError('Qualification requires its own private temporary cluster')
    expected={'host':str(area/'socket'),'port':5432,'database':DB_NAME,'user':DB_USER,'data_directory':str(area/'data')}
    if value!=expected:raise ValueError('Qualification marker differs from its owned cluster')
    for key,name in [('DB_HOST','host'),('DB_PORT','port'),('DB_NAME','database'),('DB_USER','user')]:
        if os.environ.get(key)!=str(expected[name]):raise ValueError('Qualification environment differs from marker')
    if not (area/'data/postmaster.pid').is_file():raise ValueError('Owned cluster is not running')
    from src.db import get_connection
    with get_connection() as conn,conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_user, inet_server_addr(), current_setting('data_directory')")
        database,user,network,data=cur.fetchone()
        if (database,user,network,Path(data).resolve())!=(DB_NAME,DB_USER,None,area/'data'):
            raise ValueError('Actual database is outside the owned clone')
        if conn.info.host!=expected['host'] or conn.info.port!=5432:
            raise ValueError('Actual connection is outside the owned socket')
    return value


@contextmanager
def providers_disabled():
    """Restore the caller's provider boundaries on every success/failure path."""
    import boto3
    import boto3.session
    from src import embeddings
    def refused(*args,**kwargs):raise RuntimeError('Providers are disabled during release qualification')
    targets=((boto3,'client'),(boto3,'resource'),(boto3.session.Session,'client'),
             (boto3.session.Session,'resource'),(embeddings,'_get_client'))
    originals=[(obj,name,getattr(obj,name)) for obj,name in targets]
    try:
        for obj,name in targets:setattr(obj,name,refused)
        yield
    finally:
        for obj,name,value in originals:setattr(obj,name,value)


def qualify_data(marker,output,usd_per_million_tokens):
    """No transport calls: apply unpaid repairs, then write the paid-work plan."""
    assert_target(marker)
    from src import db
    with providers_disabled():
        try:
            return _qualify_data(output,usd_per_million_tokens)
        finally:
            db.close_pool()


def _qualify_data(output,usd_per_million_tokens):
    from src import db
    from src.db import maintenance as m
    def counts():
        with db.get_connection() as conn,conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM memories), (SELECT count(*) FROM memory_relationships), (SELECT count(*) FROM schema_migrations)")
            return dict(zip(('memories','relationships','migrations'),cur.fetchone()))
    before=counts()
    # Project is part of source identity. Normalize audited UUID placeholders
    # before dedup so that this repair cannot introduce duplicates afterward.
    metadata=m.plan_metadata_repairs();metadata_sha=m.write_manifest(metadata,output/'metadata.json')
    repaired=m.apply_metadata_repairs(metadata,metadata_sha)
    if m.plan_metadata_repairs()['operations']:raise RuntimeError('Metadata repairs are incomplete')
    dedup=m.plan_dedup();dedup_sha=m.write_manifest(dedup,output/'dedup.json')
    applied=m.apply_dedup(dedup,dedup_sha)
    if m.plan_dedup()['operations']:raise RuntimeError('Duplicate sources remain after qualification')
    rechunk=m.plan_rechunk(strip_known_ide_context=True);rechunk_sha=m.write_manifest(rechunk,output/'rechunk.json')
    code={str(p.relative_to(ROOT)):backup.digest(p) for folder,pattern in [('src','*.py'),('migrations','*.sql'),('scripts/maintenance','*.py')]
          for p in sorted((ROOT/folder).rglob(pattern))}
    report={'migrations_and_repairs_qualified':True,'provider_calls':0,'live_database_modified':False,
        'manifests_target_disposable_clone':True,'before':before,'after':counts(),
        'dedup_applied':applied,'metadata_applied':repaired,'rechunk_operations':len(rechunk['operations']),
        'planned_embedding_calls_upper_bound':rechunk['embedding_calls'],'input_characters':rechunk['input_characters'],
        'strip_known_ide_context':rechunk['strip_known_ide_context'],
        'estimated_input_tokens':math.ceil(rechunk['input_characters']/4),
        'estimated_embedding_usd':math.ceil(rechunk['input_characters']/4)*usd_per_million_tokens/1_000_000,
        'pricing':{'usd_per_million_tokens':usd_per_million_tokens,'rate_source':'explicit operator-supplied planning rate',
                   'token_basis':'input characters / 4, an estimate, not a tokenizer count or cost ceiling',
                   'model':rechunk['embedding_model'],'excludes':'retries, network, storage; cache reuse may reduce calls'},
        'manifest_hashes':{'dedup':dedup_sha,'metadata':metadata_sha,'rechunk':rechunk_sha},
        'code_tree_sha256':hashlib.sha256(json.dumps(code,sort_keys=True).encode()).hexdigest()}
    private_json(output/'repair-result.json',report)
    return report


def qualify(archive,key,output,expected_sha256,*,usd_per_million_tokens,timeout_seconds=7200):
    for label,value in [('timeout',timeout_seconds),('planning rate',usd_per_million_tokens)]:
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError(f'{label} must be finite and positive')
    archive=Path(archive)
    if archive.is_symlink() or not archive.is_file() or backup.digest(archive)!=expected_sha256:
        raise ValueError('Backup hash differs from the selected encrypted bundle')
    key=backup.check_key(key)
    pg_bin=Path(os.environ.get('SECOND_BRAIN_TEST_PG_BIN','/opt/homebrew/opt/postgresql@17/bin'))
    for name in ('initdb','pg_ctl','createdb','psql','pg_restore'):
        if not (pg_bin/name).is_file():raise ValueError(f'Missing PostgreSQL tool: {name}')
    sandbox=Path('/usr/bin/sandbox-exec')
    if not sandbox.is_file():raise ValueError('Qualification requires outbound-network isolation')
    output=Path(output).resolve();output.mkdir(parents=True,mode=0o700,exist_ok=False)
    area=Path(tempfile.mkdtemp(prefix='sbqual-',dir='/private/tmp'))
    data=area/'data';(area/'socket').mkdir(mode=0o700);(area/'home').mkdir(mode=0o700)
    env=isolated_environment(area,pg_bin);deadline=time.monotonic()+timeout_seconds
    cancelled=[None];started=False;stopped=False
    def interrupted(signum,_frame):
        if cancelled[0] is None:cancelled[0]=signum
    previous={sig:signal.signal(sig,interrupted) for sig in (signal.SIGTERM,signal.SIGINT)}
    def run(args,**kwargs):
        return run_process(args,cwd=ROOT,env=env,interrupted=cancelled,timeout=deadline-time.monotonic(),**kwargs)
    # The decrypt/list helpers need no credentials; bind their command runner to
    # this same bounded, credential-free process boundary for this invocation.
    original_command=backup.command
    backup.command=lambda args,**kwargs:run(args,capture_output=True,**kwargs)
    try:
        payload,manifest=backup.extract_bundle(archive.resolve(),key,area)
        print(json.dumps({'phase':'restore','backup_payload_verified':True}),flush=True)
        logfile=os.open(output/'qualification.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        postgres_log=os.open(output/'postgres.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(postgres_log)
        with os.fdopen(logfile,'w') as log:
            run([pg_bin/'initdb','-D',data,'-U',DB_USER,'--auth-local=trust','--auth-host=reject','--no-locale','-E','UTF8'],stdout=log,stderr=subprocess.STDOUT)
            with (data/'postgresql.conf').open('a') as conf:
                conf.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{area/'socket'}'\nmaintenance_work_mem = '512MB'\n")
            run([pg_bin/'pg_ctl','-D',data,'-l',output/'postgres.log','-w','start'],allow_background=True,stdout=log,stderr=subprocess.STDOUT);started=True
            run([pg_bin/'createdb',DB_NAME],stdout=log,stderr=subprocess.STDOUT)
            run([pg_bin/'pg_restore','--exit-on-error','--no-owner','--no-acl','-d',DB_NAME,payload/'database.dump'],stdout=log,stderr=subprocess.STDOUT)
            marker=area/'qualification.json'
            private_json(marker,{'host':str(area/'socket'),'port':5432,'database':DB_NAME,'user':DB_USER,'data_directory':str(data)})
            print(json.dumps({'phase':'migrations','full_restore_verified':True}),flush=True)
            run(['/bin/bash',ROOT/'migrations/migrate.sh'],stdout=log,stderr=subprocess.STDOUT)
            policy=area/'offline.sb'
            policy.write_text('(version 1)\n(allow default)\n(deny network-outbound)\n'
                f'(allow network-outbound (remote unix-socket (path-prefix "{area/"socket"}")))\n')
            print(json.dumps({'phase':'unpaid_repairs_and_rechunk_plan'}),flush=True)
            run([sandbox,'-f',policy,sys.executable,Path(__file__).resolve(),'_worker',marker,'--output',output,
                 '--usd-per-million-tokens',str(usd_per_million_tokens)],stdout=log,stderr=subprocess.STDOUT)
        report=json.loads((output/'repair-result.json').read_text())
        report.update(archive_sha256=expected_sha256,full_restore_verified=True,payload_verified=True,
            file_modes_verified=manifest['version']>=2,knowledge_sources_included=manifest.get('knowledge_sources_included',False))
    finally:
        backup.command=original_command
        try:
            if started or (data/'postmaster.pid').exists():
                run_process([pg_bin/'pg_ctl','-D',data,'-m','immediate','-w','stop'],env=env,timeout=30,capture_output=True)
            stopped=True
        finally:
            for sig,handler in previous.items():signal.signal(sig,handler)
            if stopped:shutil.rmtree(area)
            else:print(json.dumps({'cluster_cleanup_failed':True,'private_evidence':str(area)}),file=sys.stderr)
    report['cluster_stopped']=True;report['plaintext_clone_removed']=True
    private_json(output/'qualification.json',report)
    return report


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='mode',required=True)
    q=sub.add_parser('run');q.add_argument('archive',type=Path);q.add_argument('--key',type=Path,required=True)
    q.add_argument('--sha256',required=True);q.add_argument('--timeout-seconds',type=float,default=7200)
    w=sub.add_parser('_worker');w.add_argument('marker',type=Path)
    for parser in (q,w):
        parser.add_argument('--output',type=Path,required=True)
        parser.add_argument('--usd-per-million-tokens',type=float,required=True,help='Explicit current planning rate; no paid embedding is performed')
    a=p.parse_args()
    try:
        result=(qualify(a.archive,a.key,a.output,a.sha256,usd_per_million_tokens=a.usd_per_million_tokens,timeout_seconds=a.timeout_seconds)
                if a.mode=='run' else qualify_data(a.marker,a.output,a.usd_per_million_tokens))
        print(json.dumps(result,sort_keys=True));return 0
    except TrialInterrupted as exc:
        print(json.dumps({'status':'interrupted','signal':exc.signum}));return 128+exc.signum
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'message':str(exc)}));return 1

if __name__=='__main__':raise SystemExit(main())
