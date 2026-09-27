"""Release rehearsal cannot select an existing database or call a provider."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.maintenance import qualify_release as release
from src import db


def test_child_environment_has_only_owned_target_and_no_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('AWS_ACCESS_KEY_ID','synthetic-secret')
    monkeypatch.setenv('PGSERVICE','production')
    monkeypatch.setenv('DB_HOST','production.example')
    env=release.isolated_environment(tmp_path,Path('/synthetic/pg'))
    assert env['DB_HOST']==env['PGHOST']==str(tmp_path/'socket')
    assert env['DB_NAME']==release.DB_NAME
    assert env['DB_USER']==release.DB_USER
    assert not {'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','PGSERVICE','PGPASSWORD'} & env.keys()


def test_archive_hash_mismatch_precedes_any_process_or_key_read(tmp_path,monkeypatch):
    archive=tmp_path/'backup.gpg';archive.write_bytes(b'synthetic ciphertext')
    process=Mock();monkeypatch.setattr(release,'run_process',process)
    with pytest.raises(ValueError,match='Backup hash differs'):
        release.qualify(archive,tmp_path/'missing-key',tmp_path/'out','wrong',usd_per_million_tokens=.02)
    process.assert_not_called()
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('mismatch',['environment','database','socket','data_directory'])
def test_worker_checks_actual_database_not_just_marker(monkeypatch,mismatch):
    with tempfile.TemporaryDirectory(prefix='sbqual-',dir='/private/tmp') as directory:
        area=Path(directory);(area/'data').mkdir();(area/'data/postmaster.pid').touch()
        marker=area/'qualification.json'
        value={'host':str(area/'socket'),'port':5432,'database':release.DB_NAME,'user':release.DB_USER,'data_directory':str(area/'data')}
        release.private_json(marker,value)
        for key,val in release.isolated_environment(area,'/synthetic/pg').items():monkeypatch.setenv(key,val)
        if mismatch=='environment':monkeypatch.setenv('DB_HOST','production.example')
        row=[release.DB_NAME,release.DB_USER,None,str(area/'data')]
        if mismatch=='database':row[0]='production'
        if mismatch=='data_directory':row[3]='/production/data'
        cursor=Mock();cursor.fetchone.return_value=row
        @contextmanager
        def cursor_context():yield cursor
        @contextmanager
        def connection():
            yield SimpleNamespace(info=SimpleNamespace(host='/production/socket' if mismatch=='socket' else value['host'],port=5432),cursor=cursor_context)
        monkeypatch.setattr(db,'get_connection',connection)
        with pytest.raises(ValueError,match='differs|outside'):
            release.assert_target(marker)


def test_unpaid_clone_repairs_and_rechunk_plan_use_real_sql(test_schema,clean_tables,tmp_path,monkeypatch):
    from uuid import uuid4
    a=db.create_memory('source','First','a'*5000,source_type='article',source_url='test://duplicate')
    duplicate=db.create_memory('source','Second','a'*5000,source_type='article',source_url='test://duplicate')
    project=db.create_memory('source','Legacy project','text',source_type='manual',source_url='test://project',project=str(uuid4()))
    # This one seam admits the already guarded test DB; SQL maintenance is real.
    guard=Mock();monkeypatch.setattr(release,'assert_target',guard)
    import src.embeddings as embeddings
    embedding=Mock(side_effect=AssertionError('No embeddings may run'))
    monkeypatch.setattr(embeddings,'generate_embedding',embedding)
    result=release.qualify_data(tmp_path/'synthetic-marker',tmp_path,.02)
    guard.assert_called_once()
    assert result['dedup_applied']==result['metadata_applied']==1
    assert result['before']['memories']==3 and result['after']['memories']==2
    assert result['provider_calls']==0 and result['live_database_modified'] is False
    assert db.get_memory(a) and db.get_memory(duplicate) is None
    assert db.get_memory(project)['project'] is None
    assert result['planned_embedding_calls_upper_bound']==3
    assert result['estimated_input_tokens']==1251
    assert result['estimated_embedding_usd']==pytest.approx(1251*.02/1_000_000)
    assert result['manifests_target_disposable_clone'] is True
    embedding.assert_not_called()
    assert all(path.stat().st_mode&0o777==0o600 for path in tmp_path.iterdir())


@pytest.mark.parametrize('fail_migration',[False,True])
def test_owned_cluster_cleanup_and_network_guard_are_mandatory(tmp_path,monkeypatch,fail_migration):
    archive=tmp_path/'backup.gpg';archive.write_bytes(b'synthetic ciphertext')
    key=tmp_path/'key';key.write_text('synthetic-secret-key-long-enough');key.chmod(0o600)
    area=tmp_path/'area';area.mkdir();payload=area/'payload';payload.mkdir()
    (payload/'database.dump').write_bytes(b'synthetic dump')
    monkeypatch.setattr(release.tempfile,'mkdtemp',lambda **kwargs:str(area))
    monkeypatch.setattr(release.backup,'extract_bundle',lambda *args:(payload,{'version':2,'knowledge_sources_included':True}))
    events=[];output=tmp_path/'output'
    def command(args,**kwargs):
        args=list(map(str,args));name=Path(args[0]).name
        assert kwargs['env']['DB_HOST']==str(area/'socket')
        assert 'AWS_ACCESS_KEY_ID' not in kwargs['env']
        if name=='initdb':(area/'data').mkdir();(area/'data/postgresql.conf').touch()
        elif name=='pg_ctl' and args[-1]=='start':
            assert kwargs['allow_background'] is True
            (area/'data/postmaster.pid').touch();events.append('started')
        elif name=='pg_ctl' and args[-1]=='stop':events.append('stopped')
        elif name=='bash' and fail_migration:raise subprocess.CalledProcessError(1,args)
        elif name=='sandbox-exec':
            assert '_worker' in args
            policy=Path(args[args.index('-f')+1]).read_text()
            assert '(deny network-outbound)' in policy and str(area/'socket') in policy
            release.private_json(output/'repair-result.json',{'provider_calls':0})
        return subprocess.CompletedProcess(args,0,b'',b'')
    monkeypatch.setattr(release,'run_process',command)
    original=release.backup.command
    handlers={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
    if fail_migration:
        with pytest.raises(subprocess.CalledProcessError):
            release.qualify(archive,key,output,release.backup.digest(archive),usd_per_million_tokens=.02)
        assert not (output/'qualification.json').exists()
    else:
        report=release.qualify(archive,key,output,release.backup.digest(archive),usd_per_million_tokens=.02)
        assert report['full_restore_verified'] and report['file_modes_verified'] and report['knowledge_sources_included']
        assert report['cluster_stopped'] and report['plaintext_clone_removed']
    assert events==['started','stopped']
    assert not area.exists()
    assert release.backup.command is original
    assert all(signal.getsignal(sig)==handler for sig,handler in handlers.items())


@pytest.mark.parametrize('fail',[False,True])
def test_qualification_restores_all_provider_hooks_and_closes_pool(monkeypatch,tmp_path,fail):
    import boto3
    import boto3.session
    from src import embeddings
    targets=((boto3,'client'),(boto3,'resource'),(boto3.session.Session,'client'),
             (boto3.session.Session,'resource'),(embeddings,'_get_client'))
    originals=[getattr(obj,name) for obj,name in targets]
    monkeypatch.setattr(release,'assert_target',lambda *_:None)
    close=Mock();monkeypatch.setattr(db,'close_pool',close)
    def operation(*args):
        for obj,name in targets:
            with pytest.raises(RuntimeError,match='Providers are disabled during release qualification'):
                getattr(obj,name)()
        if fail:raise ValueError('synthetic qualification failure')
        return {'provider_calls':0}
    monkeypatch.setattr(release,'_qualify_data',operation)
    if fail:
        with pytest.raises(ValueError,match='synthetic qualification failure'):
            release.qualify_data(tmp_path/'marker',tmp_path,.02)
    else:
        assert release.qualify_data(tmp_path/'marker',tmp_path,.02)=={'provider_calls':0}
    assert all(getattr(obj,name) is original for (obj,name),original in zip(targets,originals))
    close.assert_called_once()


def test_project_repair_precedes_dedup_identity_planning(test_schema,clean_tables,tmp_path,monkeypatch):
    from uuid import uuid4
    from src.db.maintenance import plan_dedup
    existing=db.create_memory('source','Existing','Same source',source_type='article',source_url='test://identity-change')
    legacy=db.create_memory('source','Legacy','Same source',source_type='article',source_url='test://identity-change',project=str(uuid4()))
    assert not plan_dedup()['operations']  # They are distinct until the UUID placeholder is repaired.
    monkeypatch.setattr(release,'assert_target',lambda *_:None)
    report=release.qualify_data(tmp_path/'marker',tmp_path,.02)
    assert report['metadata_applied']==report['dedup_applied']==1
    assert report['after']['memories']==1
    assert not plan_dedup()['operations']
    assert db.get_memory(existing) and db.get_memory(legacy) is None
    assert db.get_memory(existing)['metadata']['dedup_aliases'][0]['metadata']['legacy_uuid_project']
