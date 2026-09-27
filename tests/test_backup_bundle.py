"""Exercise real encryption/decryption and source retention on failed backup steps."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import select
import signal
import subprocess
import sys
import tarfile
import textwrap
import pytest
from scripts.maintenance import backup_bundle as backup


def fake_dump(args, **kwargs):
    if args[0]=='pg_dump':
        Path(args[args.index('--file')+1]).write_bytes(b'synthetic database dump')
        return None
    if args[0]=='pg_restore':
        return None
    return _command(args, **kwargs)

_command=backup.command


def test_encrypted_roundtrip_preserves_sources_and_cleans_plaintext(tmp_path):
    key=tmp_path/'key'; key.write_bytes(b'synthetic test key never used outside tests'); key.chmod(0o600)
    repo=tmp_path/'repo'; repo.mkdir(); (repo/'README.md').write_text('private synthetic document')
    with patch.object(backup, 'command', side_effect=fake_dump):
        result=backup.create_bundle(repo,tmp_path/'backups',key)
        assert b'private synthetic document' not in Path(result['archive']).read_bytes()
        check=backup.verify_bundle(result['archive'],key)
        assert check['payload_verified'] and not check['restore_verified']
    assert (repo/'README.md').read_text()=='private synthetic document'
    assert not list((tmp_path/'backups').glob('.sb-backup-*'))


@pytest.mark.parametrize('failure', ['upload', 'download', 'mismatch'])
def test_failed_cloud_verification_retains_decryptable_local_archive(tmp_path, failure):
    key = tmp_path / 'key'
    key.write_bytes(b'synthetic test recovery passphrase')
    key.chmod(0o600)
    repo = tmp_path / 'repo'
    repo.mkdir()
    source = repo / 'README.md'
    source.write_text('private synthetic document retained on cloud failure')
    output = tmp_path / 'backups'
    cloud_calls = []

    def simulated_cloud(args, **kwargs):
        if args[0] != 'rclone':
            return fake_dump(args, **kwargs)
        cloud_calls.append(args)
        if len(cloud_calls) == 1:
            if failure == 'upload':
                raise RuntimeError('synthetic upload failure')
        elif failure == 'download':
            raise RuntimeError('synthetic readback failure')
        else:
            Path(args[-1]).write_bytes(b'corrupted synthetic ciphertext')
        return subprocess.CompletedProcess(args, 0, b'', b'')

    exception = ValueError if failure == 'mismatch' else RuntimeError
    with patch.object(backup, 'command', side_effect=simulated_cloud):
        with pytest.raises(exception, match='ciphertext differs|synthetic .* failure'):
            backup.create_bundle(repo, output, key, remote='synthetic:backups')

    archives = list(output.glob('*.tar.gpg'))
    assert len(archives) == 1
    archive = archives[0]
    assert archive.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() not in archive.read_bytes()
    assert not list(output.glob('*.receipt.json'))
    assert not list(output.glob('.sb-backup-*'))
    assert source.read_text() == 'private synthetic document retained on cloud failure'
    assert len(cloud_calls) == (1 if failure == 'upload' else 2)
    assert cloud_calls[0] == ['rclone', 'copyto', archive, 'synthetic:backups/' + archive.name]
    with patch.object(backup, 'command', side_effect=fake_dump):
        assert backup.verify_bundle(archive, key)['payload_verified'] is True


@pytest.mark.parametrize('interruption', ['keyboard', 'timeout'])
def test_command_kills_term_resistant_descendant_after_leader_exits(tmp_path, interruption):
    """Exercise real process-group cleanup when only an orphan holds the pipes."""
    marker = tmp_path / 'grandchild.pid'
    grandchild_code = textwrap.dedent('''
        import os, signal, sys, time
        from pathlib import Path
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(sys.argv[1]).write_text(str(os.getpid()))
        time.sleep(60)
    ''')
    leader_code = textwrap.dedent('''
        import os, subprocess, sys, time
        from pathlib import Path
        subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])
        deadline = time.monotonic() + 5
        while not Path(sys.argv[2]).exists():
            if time.monotonic() > deadline:
                raise RuntimeError('synthetic descendant did not start')
            time.sleep(0.01)
        os._exit(0)
    ''')
    real_popen = subprocess.Popen
    children = []
    pipes_closed = False

    class InterruptAfterLeaderExit(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

        def communicate(self, *args, **kwargs):
            assert self.wait(timeout=10) == 0
            assert os.getpgid(int(marker.read_text())) == self.pid
            if interruption == 'keyboard':
                raise KeyboardInterrupt
            # The exited leader's descendant still owns stdout/stderr, so this
            # is a real communicate timeout rather than a mocked exception.
            return super().communicate(timeout=0.05)

    expected = KeyboardInterrupt if interruption == 'keyboard' else subprocess.TimeoutExpired
    try:
        with patch.object(backup.subprocess, 'Popen', InterruptAfterLeaderExit):
            with pytest.raises(expected):
                backup.command([sys.executable, '-c', leader_code, grandchild_code, str(marker)])
        assert len(children) == 1
        child = children[0]
        # EOF proves the grandchild no longer holds its inherited pipe open;
        # unlike kill(pid, 0), it also handles a briefly unreaped zombie.
        assert select.select([child.stdout], [], [], 3)[0]
        assert os.read(child.stdout.fileno(), 1) == b''
        pipes_closed = True
    finally:
        for child in children:
            if not pipes_closed:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


def test_failed_encryption_cannot_upload_or_delete_source(tmp_path):
    key=tmp_path/'key'; key.write_bytes(b'synthetic backup key value'); key.chmod(0o600)
    repo=tmp_path/'repo'; repo.mkdir(); (repo/'README.md').write_text('retain me')
    with patch.object(backup,'command',side_effect=fake_dump), patch.object(backup,'gpg',side_effect=RuntimeError('encryption failed')):
        with pytest.raises(RuntimeError): backup.create_bundle(repo,tmp_path/'out',key,remote='never:upload')
    assert (repo/'README.md').read_text()=='retain me'
    assert not list((tmp_path/'out').glob('*.gpg'))


def test_payload_tampering_detected(tmp_path):
    (tmp_path/'database.dump').write_bytes(b'before')
    (tmp_path/'manifest.json').write_text(json.dumps({'version':1,'files':{'database.dump':{'bytes':6,'sha256':backup.digest(tmp_path/'database.dump')}}}))
    (tmp_path/'database.dump').write_bytes(b'after!')
    with pytest.raises(ValueError,match='checksum'): backup.validate_payload(tmp_path)


def test_symlink_secrets_are_excluded(tmp_path):
    secret=tmp_path/'secret';secret.write_text('secret')
    repo=tmp_path/'repo';repo.mkdir();(repo/'leak').symlink_to(secret)
    dest=tmp_path/'copy';backup.copy_tree(repo,dest)
    assert not (dest/'leak').exists()


def test_key_requires_private_permissions(tmp_path):
    key=tmp_path/'key';key.write_bytes(b'synthetic test passphrase');key.chmod(0o644)
    with pytest.raises(ValueError,match='0600'): backup.check_key(key)


def test_configured_symlink_knowledge_root_fails(tmp_path):
    key=tmp_path/'key';key.write_bytes(b'synthetic test passphrase');key.chmod(0o600)
    repo=tmp_path/'repo';repo.mkdir()
    actual=tmp_path/'actual';actual.mkdir();(actual/'source.md').write_text('knowledge')
    link=tmp_path/'link';link.symlink_to(actual,target_is_directory=True)
    with patch.object(backup,'command',side_effect=fake_dump):
        with pytest.raises(ValueError,match='real directory'):
            backup.create_bundle(repo,tmp_path/'out',key,kb=link)
    assert not list((tmp_path/'out').glob('*.gpg'))


def test_copy_preserves_owner_execution_without_public_permissions(tmp_path):
    source=tmp_path/'job.sh';source.write_text('#!/bin/sh\nexit 0\n');source.chmod(0o755)
    target=tmp_path/'restored'/'job.sh';backup.copy_tree(source,target)
    assert target.stat().st_mode & 0o777==0o700


def test_manifest_rejects_lost_executable_permission(tmp_path):
    dump=tmp_path/'database.dump';dump.write_bytes(b'dump');dump.chmod(0o600)
    script=tmp_path/'script.sh';script.write_text('exit 0');script.chmod(0o600)
    files={p.name:{'sha256':backup.digest(p),'bytes':p.stat().st_size,'mode':0o700 if p==script else 0o600} for p in (dump,script)}
    (tmp_path/'manifest.json').write_text(json.dumps({'version':2,'files':files}))
    with pytest.raises(ValueError,match='permissions'):backup.validate_payload(tmp_path)
