"""Synthetic process trees prove cancellation precedes cluster teardown."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from scripts.eval import discovery_trial as launcher


def _tree(tmp_path, *, interrupt=False, exit_leader=False, ignore_term=False):
    grandchild = tmp_path/'grandchild.py'
    grandchild.write_text("import os,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n")
    child = tmp_path/'child.py'
    child.write_text("import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable,str(Path(__file__).with_name('grandchild.py'))])\ntime.sleep(30)\n")
    worker = tmp_path/'worker.py'
    worker.write_text("import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\n" if ignore_term else "")
        + "Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable,str(Path(__file__).with_name('child.py'))])\n"
        "while not Path(__file__).with_name('grandchild.pid').exists():time.sleep(.01)\n"
        + ("os.kill(os.getppid(),signal.SIGTERM)\ntime.sleep(.05)\nos.kill(os.getppid(),signal.SIGTERM)\n" if interrupt else "")
        + ("sys.exit(0)\n" if exit_leader else "time.sleep(30)\n"))
    return worker


def _assert_tree_stopped(tmp_path):
    for name in ('worker','child','grandchild'):
        pid=int((tmp_path/(name+'.pid')).read_text())
        deadline=time.monotonic()+2
        while True:
            try:os.kill(pid,0)
            except ProcessLookupError:break
            except PermissionError:pass  # macOS may briefly deny probing adopted zombies.
            assert time.monotonic()<deadline, f'{name} survived process-group shutdown'
            time.sleep(.01)


def test_worker_deadline_stops_term_ignoring_child_and_grandchild(tmp_path):
    worker=_tree(tmp_path,ignore_term=True)
    started=time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        launcher.run_process([sys.executable,worker],timeout=.5,capture_output=True)
    assert time.monotonic()-started < 10
    _assert_tree_stopped(tmp_path)


def test_exited_worker_does_not_leave_pipe_holding_descendants(tmp_path):
    worker=_tree(tmp_path,exit_leader=True)
    result=launcher.run_process([sys.executable,worker],timeout=5,capture_output=True)
    assert result.returncode==0
    _assert_tree_stopped(tmp_path)


@pytest.mark.parametrize('phase',['worker','bootstrap'])
def test_launcher_sigterm_cleans_whole_tree_before_cluster_stop(tmp_path,monkeypatch,phase):
    # Bootstrap is synthetic. Only the owned worker process tree actually runs.
    # No second PostgreSQL cluster, private records, or providers are contacted.
    area=tmp_path/'area';area.mkdir()
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'synthetic':True}))
    worker=_tree(tmp_path,interrupt=True,ignore_term=True)
    monkeypatch.setenv('CLAUDE_CODE_USE_BEDROCK','1')
    monkeypatch.setattr(launcher.tempfile,'mkdtemp',lambda **kwargs:str(area))
    real_run=launcher.run_process
    events=[]
    def fake_bootstrap(command,**kwargs):
        args=list(map(str,command));name=Path(args[0]).name
        if '_worker' in args or (phase=='bootstrap' and name=='pg_ctl' and args[-1]=='start'):
            assert ('_worker' in args)==(phase=='worker')
            (area/'data'/'postmaster.pid').write_text('synthetic')
            events.append('process_started')
            return real_run([sys.executable,worker],**kwargs)
        if name=='initdb':
            (area/'data').mkdir()
            (area/'data'/'postgresql.conf').touch()
        elif name=='pg_ctl' and args[-1]=='stop':
            _assert_tree_stopped(tmp_path)
            events.append('cluster_stopped')
        elif name=='pg_ctl':
            (area/'data'/'postmaster.pid').write_text('synthetic')
        elif name=='psql':
            if args[-1]=='SHOW data_directory':
                return subprocess.CompletedProcess(args,0,str(area/'data')+'\n','')
        else:
            assert name=='bash'
        return subprocess.CompletedProcess(args,0,'','')
    monkeypatch.setattr(launcher,'run_process',fake_bootstrap)
    old_handlers={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
    started=time.monotonic()
    with pytest.raises(launcher.TrialInterrupted) as cancelled:
        launcher.run_isolated(manifest,tmp_path/'output',launcher.sha({'synthetic':True}),timeout_seconds=20)
    assert cancelled.value.signum==signal.SIGTERM
    assert events==['process_started','cluster_stopped']
    assert time.monotonic()-started < 10
    assert all(signal.getsignal(sig)==handler for sig,handler in old_handlers.items())
    _assert_tree_stopped(tmp_path)


@pytest.mark.parametrize('value',[0,-1,float('nan'),float('inf'),True])
def test_invalid_deadline_rejected_before_read_or_launch(tmp_path,monkeypatch,value):
    from unittest.mock import Mock
    start=Mock();monkeypatch.setattr(launcher.subprocess,'Popen',start)
    with pytest.raises(ValueError,match='finite and positive'):
        launcher.run_isolated(tmp_path/'missing',tmp_path/'out','unused',timeout_seconds=value)
    start.assert_not_called()
