#!/usr/bin/env python3
"""Prepare a reviewable transfer manifest or run it in a fresh evaluation cluster.

prepare makes no model calls. run requires the exact manifest hash and explicit
Bedrock routing for the Explorer. It never reuses an installed database, launches
email, mutates source captures, or supplies human outcome ratings.
"""
from __future__ import annotations
import argparse
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
from src.evaluation.trial import prepare,sha

DEFAULT_TIMEOUT_SECONDS = 3600
BOOTSTRAP_TIMEOUT_SECONDS = 300
STOP_GRACE_SECONDS = 2
KILL_WAIT_SECONDS = 5


class TrialInterrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


def _signal_group(child, signum):
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _stop_group(child):
    """Terminate descendants even when their group leader has already exited."""
    _signal_group(child, signal.SIGTERM)
    try:
        child.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # A grandchild may ignore TERM after its parent has already exited.
    _signal_group(child, signal.SIGKILL)
    child.wait(timeout=KILL_WAIT_SECONDS)


def run_process(command, *, timeout, interrupted=None, check=True,
                allow_background=False, capture_output=False, **kwargs):
    """Bound one owned process group, including launch failures and cancellation.

    Signal handlers only record cancellation, so a signal during Popen cannot
    strand a child before its handle is assigned. Polling observes it within
    100ms. Only successful pg_ctl start may intentionally retain a background
    server; every other command must finish with its descendants stopped.
    """
    if interrupted is not None and interrupted[0] is not None:
        raise TrialInterrupted(interrupted[0])
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, timeout)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child = subprocess.Popen([str(c) for c in command], start_new_session=True, **kwargs)
    completed = False
    group_stopped = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if interrupted is not None and interrupted[0] is not None:
                raise TrialInterrupted(interrupted[0])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = child.communicate(timeout=min(.1, remaining))
                break
            except subprocess.TimeoutExpired:
                if child.poll() is not None and not allow_background:
                    # Orphans can retain the pipe after their leader exits.
                    _stop_group(child)
                    group_stopped = True
                    stdout, stderr = child.communicate(timeout=KILL_WAIT_SECONDS)
                    break
        if interrupted is not None and interrupted[0] is not None:
            raise TrialInterrupted(interrupted[0])
        if check and child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command, stdout, stderr)
        completed = True
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    finally:
        if not group_stopped and not (completed and allow_background):
            _stop_group(child)


def run_isolated(manifest_path, output, approved_hash, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('Trial timeout must be finite and positive')
    deadline = time.monotonic() + timeout_seconds
    manifest=json.loads(manifest_path.read_text())
    if sha(manifest)!=approved_hash:raise ValueError('Manifest hash differs from reviewed transfer')
    if os.environ.get('CLAUDE_CODE_USE_BEDROCK')!='1':raise ValueError('Set and verify CLAUDE_CODE_USE_BEDROCK=1 first')
    pg_bin=Path(os.environ.get('SECOND_BRAIN_TEST_PG_BIN','/opt/homebrew/opt/postgresql@17/bin'))
    for name in ('initdb','pg_ctl','psql'):
        if not (pg_bin/name).is_file():raise ValueError(f'Missing PostgreSQL tool: {name}')
    area=Path(tempfile.mkdtemp(prefix='sbeval-',dir='/private/tmp'))
    data,sockets=area/'data',area/'socket';sockets.mkdir(mode=0o700)
    # Native provider credentials remain in the explicitly approved process; data
    # and MCP targets are redirected before importing any application module.
    env=dict(os.environ)
    for key in tuple(env):
        if key.startswith('DB_') or key.startswith('PG'):env.pop(key)
    env.update(PATH=f'{pg_bin}:'+env.get('PATH',''),PYTHONPATH=str(ROOT),SECOND_BRAIN_PROFILE='controlled',
               DB_HOST=str(sockets),DB_PORT='5432',DB_USER='sb_eval',DB_NAME='sb_eval_trial',DB_PASSWORD='',
               PGPASSFILE=str(area/'absent-pgpass'),KIRO_LLM_METRICS_DIR=str(output/'metrics'))
    started=False
    cancelled=[None]
    def run(cmd,**kwargs):
        return run_process(cmd,cwd=ROOT,env=env,interrupted=cancelled,
            timeout=min(BOOTSTRAP_TIMEOUT_SECONDS,deadline-time.monotonic()),**kwargs)
    def interrupted(signum,frame):
        if cancelled[0] is None:cancelled[0]=signum
    previous={sig:signal.signal(sig,interrupted) for sig in (signal.SIGTERM,signal.SIGINT)}
    try:
        with (area/'bootstrap.log').open('w') as log:
            run([pg_bin/'initdb','-D',data,'-U','sb_eval_owner','--auth-local=trust','--auth-host=reject','--no-locale','-E','UTF8'],stdout=log,stderr=subprocess.STDOUT)
            with (data/'postgresql.conf').open('a') as f:f.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{sockets}'\n")
            run([pg_bin/'pg_ctl','-D',data,'-l',area/'postgres.log','-w','start'],allow_background=True,stdout=log,stderr=subprocess.STDOUT);started=True
            psql=[pg_bin/'psql','-X','-v','ON_ERROR_STOP=1','-h',sockets,'-U','sb_eval_owner']
            for sql in ["CREATE ROLE sb_eval LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE", "CREATE DATABASE sb_eval_trial OWNER sb_eval"]:
                run(psql+['-d','postgres','-c',sql],stdout=log,stderr=subprocess.STDOUT)
            run(psql+['-d','sb_eval_trial','-c','CREATE EXTENSION vector'],stdout=log,stderr=subprocess.STDOUT)
            actual=run(psql+['-d','sb_eval_trial','-Atc','SHOW data_directory'],capture_output=True,text=True).stdout.strip()
            if Path(actual).resolve()!=data:raise ValueError('Unexpected cluster identity')
            run(['/bin/bash',ROOT/'migrations/migrate.sh'],stdout=log,stderr=subprocess.STDOUT)
        marker=area/'evaluation.json';marker.write_text(json.dumps({'host':str(sockets),'port':5432,'user':'sb_eval','databases':['sb_eval_trial'],'data_directory':str(data)}))
        env['SECOND_BRAIN_EVALUATION_MANIFEST']=str(marker)
        result=run_process([sys.executable,str(Path(__file__).resolve()),'_worker',str(manifest_path.resolve()),'--output',str(output),'--approved-sha256',approved_hash],
            cwd=ROOT,env=env,check=False,interrupted=cancelled,timeout=deadline-time.monotonic())
        return result.returncode
    finally:
        try:
            if started or (data/'postmaster.pid').exists():
                # Worker/bootstrap group cleanup has completed before this point.
                # Repeated cancellation cannot interrupt this bounded shutdown.
                run_process([pg_bin/'pg_ctl','-D',data,'-m','immediate','-w','stop'],
                    env=env,capture_output=True,text=True,timeout=30)
            print(json.dumps({'cluster_stopped':True,'private_evidence':str(area)}))
        finally:
            for sig,handler in previous.items():signal.signal(sig,handler)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('records',type=Path);prep.add_argument('--task',required=True);prep.add_argument('--output',type=Path,required=True);prep.add_argument('--cap-usd',type=float,default=5)
    for mode in ('run','_worker'):
        p=sub.add_parser(mode);p.add_argument('manifest',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--approved-sha256',required=True)
        if mode=='run':p.add_argument('--timeout-seconds',type=float,default=DEFAULT_TIMEOUT_SECONDS,help='Overall bootstrap and worker deadline (default: 3600 seconds)')
    args=parser.parse_args()
    try:
        if args.mode=='prepare':
            data=prepare(args.task,json.loads(args.records.read_text()),cap_usd=args.cap_usd)
            with args.output.open('x') as f:f.write(json.dumps(data,indent=2)+'\n')
            print(json.dumps({'manifest':str(args.output),'sha256':sha(data),'records':len(data['records']),'model_calls':0}));return 0
        if args.mode=='run':return run_isolated(args.manifest,args.output.resolve(),args.approved_sha256,timeout_seconds=args.timeout_seconds)
        from src.evaluation.trial import run_trial
        result=run_trial(json.loads(args.manifest.read_text()),args.output,approved_sha256=args.approved_sha256)
        print(json.dumps(result));return 2 if result['aborted'] else 0
    except TrialInterrupted as exc:
        print(json.dumps({'status':'interrupted','signal':exc.signum}),file=sys.stderr);return 128+exc.signum
    except subprocess.TimeoutExpired:
        print(json.dumps({'status':'deadline_exceeded'}),file=sys.stderr);return 124
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'message':str(exc)}),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
