"""scripts/maintenance/backup_bundle.py — real encryption/decryption, retention, and
an end-to-end create → verify → restore roundtrip against disposable databases.

Rules pinned here (E-N2, E-N3, E-N9, F-135): nothing but ``*.gpg`` is ever handed
to rclone; retention prunes ciphertext only and only after verification; the
knowledge sources come from config by default; the newest backup of EITHER naming
scheme is selected and must be fresh; the bundle restores with identical row counts.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tarfile
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.maintenance import backup_bundle as backup

_command = backup.command


def fake_dump(args, **kwargs):
    if args[0] == 'pg_dump':
        Path(args[args.index('--file') + 1]).write_bytes(b'synthetic database dump')
        return None
    if args[0] == 'pg_restore':
        return None
    return _command(args, **kwargs)


def _key(tmp_path):
    key = tmp_path / 'key'
    key.write_bytes(b'synthetic test key never used outside tests')
    key.chmod(0o600)
    return key


def _repo(tmp_path, text='private synthetic document'):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'README.md').write_text(text)
    (repo / '.env').write_text('CAPTURE_API_TOKEN=synthetic-token-value\n')
    return repo


# ----------------------------------------------------------------- receipt name (S3-3)

def test_receipt_is_named_second_brain_stamp_receipt_json_next_to_the_bundle(tmp_path):
    """Docs, runbook and retention say ``second-brain_<stamp>.receipt.json``; the code
    used ``Path.with_suffix`` on ``.tar.gpg`` and wrote ``…tar.receipt.json``."""
    key, repo = _key(tmp_path), _repo(tmp_path)
    with patch.object(backup, 'command', side_effect=fake_dump):
        result = backup.create_bundle(repo, tmp_path / 'backups', key)
    archive = Path(result['archive'])
    assert backup.BUNDLE_NAME.match(archive.name)
    stamp = archive.name[len('second-brain_'):-len('.tar.gpg')]
    receipt = archive.with_name(f'second-brain_{stamp}.receipt.json')
    assert receipt.is_file() and result['receipt'] == str(receipt)
    assert receipt.stat().st_mode & 0o077 == 0
    assert json.loads(receipt.read_text())['archive'] == str(archive)
    produced = sorted(p.name for p in (tmp_path / 'backups').iterdir())
    assert produced == sorted([archive.name, receipt.name])
    assert not any(name.endswith('.tar.receipt.json') for name in produced)
    assert backup.receipt_path('x/second-brain_20261006T020000000000Z.tar.gpg') == Path('x/second-brain_20261006T020000000000Z.receipt.json')
    with pytest.raises(ValueError):
        backup.receipt_path('memory_bank_20261006_020005.dump.gpg')


# --------------------------------------------------------------- roundtrip (fake DB)

def test_encrypted_roundtrip_preserves_sources_and_cleans_plaintext(tmp_path):
    key, repo = _key(tmp_path), _repo(tmp_path)
    with patch.object(backup, 'command', side_effect=fake_dump):
        result = backup.create_bundle(repo, tmp_path / 'backups', key)
        archive = Path(result['archive']).read_bytes()
        assert b'private synthetic document' not in archive and b'synthetic-token-value' not in archive
        check = backup.verify_bundle(result['archive'], key)
        assert check['payload_verified'] and not check['restore_verified'] and check['format'] == 'bundle'
    assert (repo / 'README.md').read_text() == 'private synthetic document'
    assert not list((tmp_path / 'backups').glob('.sb-backup-*'))
    receipt = json.loads(backup.receipt_path(result['archive']).read_text())
    assert receipt['remote_verified'] is False and receipt['knowledge_sources_included'] is False
    assert 'repository/.env' in {m for m in _members(result['archive'], key)}  # .env rides inside, encrypted


def test_verify_scratch_dir_lives_under_temp_root_and_is_removed(tmp_path):
    """``temp_root`` places verify's plaintext scratch dir (decrypted tar + payload)
    under the caller's directory and it is gone afterwards; without it the default
    parent stays /private/tmp (when present), unchanged for production callers."""
    key, repo = _key(tmp_path), _repo(tmp_path)
    verify_root = tmp_path / 'verify-root'
    verify_root.mkdir()
    seen = []
    real_extract = backup.extract_bundle

    def spy(archive, key, area):
        seen.append(Path(area))
        assert (Path(area) / 'decrypted.tar').exists() is False
        root, manifest = real_extract(archive, key, area)
        assert (Path(area) / 'decrypted.tar').exists()   # plaintext really lands here
        return root, manifest
    with patch.object(backup, 'command', side_effect=fake_dump):
        result = backup.create_bundle(repo, tmp_path / 'backups', key)
        with patch.object(backup, 'extract_bundle', side_effect=spy):
            assert backup.verify_bundle(result['archive'], key, temp_root=verify_root)['payload_verified']
    assert len(seen) == 1 and seen[0].parent == verify_root and seen[0].name.startswith('sb-verify-')
    assert not seen[0].exists() and not list(verify_root.iterdir())
    expected_default = '/private/tmp' if Path('/private/tmp').exists() else None
    assert backup._verify_temp_root() == expected_default
    assert backup._verify_temp_root(verify_root) == str(verify_root)


def _members(archive, key):
    with patch.object(backup, 'command', side_effect=fake_dump):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root, manifest = backup.extract_bundle(Path(archive), key, temp)
            return set(manifest['files'])


# ------------------------------------------------------------------ cloud safety

@pytest.mark.parametrize('failure', ['upload', 'download', 'mismatch'])
def test_failed_cloud_verification_retains_decryptable_local_archive_and_prunes_nothing_remote(tmp_path, failure):
    key, repo = _key(tmp_path), _repo(tmp_path, 'private synthetic document retained on cloud failure')
    output = tmp_path / 'backups'
    cloud_calls = []

    def simulated_cloud(args, **kwargs):
        if args[0] != 'rclone':
            return fake_dump(args, **kwargs)
        cloud_calls.append([str(a) for a in args])
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
            backup.create_bundle(repo, output, key, remote='synthetic:backups', local_retention_days=7, remote_retention_days=30)

    archives = list(output.glob('*.tar.gpg'))
    assert len(archives) == 1
    archive = archives[0]
    assert archive.stat().st_mode & 0o777 == 0o600
    assert (repo / 'README.md').read_bytes() not in archive.read_bytes()
    assert not list(output.glob('*.receipt.json'))
    assert not list(output.glob('.sb-backup-*'))
    assert len(cloud_calls) == (1 if failure == 'upload' else 2)
    assert cloud_calls[0] == ['rclone', 'copyto', str(archive), 'synthetic:backups/' + archive.name]
    assert not any(c[1] == 'delete' for c in cloud_calls), 'a failed upload must never prune the remote'
    with patch.object(backup, 'command', side_effect=fake_dump):
        assert backup.verify_bundle(archive, key)['payload_verified'] is True


def test_only_the_encrypted_bundle_is_ever_passed_to_rclone(tmp_path):
    """Every rclone invocation: copyto of the .tar.gpg (up, then back), and a delete
    filtered to *.gpg at depth 1. No sync, no plaintext path, no repo path."""
    key, repo = _key(tmp_path), _repo(tmp_path)
    kb = tmp_path / 'kb'
    kb.mkdir()
    (kb / 'doc.md').write_text('knowledge plaintext')
    output = tmp_path / 'backups'
    rclone_calls = []

    def recording(args, **kwargs):
        if args[0] == 'rclone':
            argv = [str(a) for a in args]
            rclone_calls.append(argv)
            if argv[1] == 'copyto' and argv[2].startswith('synthetic:'):
                Path(argv[3]).write_bytes(Path(argv[2].split('/')[-1] and (output / argv[2].split('/')[-1])).read_bytes())
            return subprocess.CompletedProcess(args, 0, b'', b'')
        return fake_dump(args, **kwargs)

    with patch.object(backup, 'command', side_effect=recording):
        result = backup.create_bundle(repo, output, key, kb=kb, remote='synthetic:backups', local_retention_days=7, remote_retention_days=30)
    assert result['remote_verified'] is True and result['pruned_remote'] is True and result['knowledge_sources_included'] is True
    assert len(rclone_calls) == 3
    up, down, prune = rclone_calls
    assert up[1] == 'copyto' and up[2].endswith('.tar.gpg') and up[2] == result['archive'] and up[3] == 'synthetic:backups/' + Path(up[2]).name
    assert down[1] == 'copyto' and down[2] == up[3] and down[3].endswith('downloaded.gpg')
    assert prune == ['rclone', 'delete', 'synthetic:backups/', '--min-age', '30d', '--include', '*.gpg', '--max-depth', '1']
    for argv in rclone_calls:
        assert argv[1] != 'sync'
        for arg in argv[2:]:
            if '/' in arg and not arg.startswith('synthetic:'):        # every local path handed to rclone
                assert arg.endswith('.gpg'), argv
            assert str(repo) not in arg and str(kb) not in arg and 'kb-sources' not in arg and 'config' not in arg


def test_rclone_remote_prune_is_skipped_when_retention_is_disabled_or_no_remote(tmp_path):
    key, repo = _key(tmp_path), _repo(tmp_path)
    calls = []

    def recording(args, **kwargs):
        if args[0] == 'rclone':
            calls.append([str(a) for a in args])
            if str(args[2]).startswith('synthetic:'):
                Path(args[3]).write_bytes(Path(args[2].split('/')[-1] and (tmp_path / 'out' / args[2].split('/')[-1])).read_bytes())
            return subprocess.CompletedProcess(args, 0, b'', b'')
        return fake_dump(args, **kwargs)
    with patch.object(backup, 'command', side_effect=recording):
        r = backup.create_bundle(repo, tmp_path / 'out', key, remote='synthetic:backups', remote_retention_days=0)
        assert r['pruned_remote'] is False and [c[1] for c in calls] == ['copyto', 'copyto']
        calls.clear()
        r = backup.create_bundle(repo, tmp_path / 'out', key, remote=None, remote_retention_days=30)
        assert r['pruned_remote'] is False and calls == []


# -------------------------------------------------------------------- retention

def _aged(path: Path, days: float, content=b'x'):
    path.write_bytes(content)
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_local_retention_prunes_old_ciphertext_of_both_schemes_and_keeps_recent_and_new(tmp_path):
    output = tmp_path / 'backups'
    output.mkdir()
    old_bundle = _aged(output / 'second-brain_20260901T020000000000Z.tar.gpg', 10)
    old_receipt = _aged(output / 'second-brain_20260901T020000000000Z.receipt.json', 10)
    old_legacy = _aged(output / 'memory_bank_20260901_020005.dump.gpg', 10)
    old_json = _aged(output / 'memories_20260901_020005.json.gpg', 10)
    recent = _aged(output / 'memory_bank_20261004_020005.dump.gpg', 2)
    plaintext = _aged(output / 'notes.txt', 40)                       # never touched: not ciphertext
    sub = output / 'kb-sources'
    sub.mkdir()
    nested = _aged(sub / 'old.gpg', 40)                                 # subdirectories are out of scope
    new = _aged(output / 'second-brain_20261006T020000000000Z.tar.gpg', 10)   # 'keep' wins even with an old mtime
    removed = backup.prune_local(output, 7, keep=[new])
    assert sorted(removed) == sorted([old_bundle.name, old_receipt.name, old_legacy.name, old_json.name])
    assert recent.exists() and plaintext.exists() and nested.exists() and new.exists()
    assert backup.prune_local(output, 0) == [] and recent.exists()


def test_create_prunes_local_only_after_the_new_bundle_is_verified(tmp_path):
    key, repo = _key(tmp_path), _repo(tmp_path)
    output = tmp_path / 'backups'
    output.mkdir()
    stale = _aged(output / 'memory_bank_20260101_020005.dump.gpg', 30)
    with patch.object(backup, 'command', side_effect=fake_dump), patch.object(backup, 'gpg', side_effect=RuntimeError('encryption failed')):
        with pytest.raises(RuntimeError):
            backup.create_bundle(repo, output, key, local_retention_days=7)
    assert stale.exists(), 'a failed bundle must not prune anything'
    with patch.object(backup, 'command', side_effect=fake_dump):
        result = backup.create_bundle(repo, output, key, local_retention_days=7)
    assert result['pruned_local'] == [stale.name] and not stale.exists()
    assert Path(result['archive']).exists() and result['retention'] == {'local_days': 7, 'remote_days': 0}


# ---------------------------------------------------------------------- config

def test_config_supplies_the_knowledge_source_path_and_retention_defaults(tmp_path, monkeypatch):
    for var in ('SECOND_BRAIN_KB_SOURCES', 'SECOND_BRAIN_BACKUP_REMOTE', 'SECOND_BRAIN_BACKUP_LOCAL_RETENTION_DAYS'):
        monkeypatch.delenv(var, raising=False)
    cfg = backup.load_config(Path(__file__).resolve().parents[1] / 'config' / 'backup.toml')
    assert cfg['kb_sources'] == Path(os.path.expanduser('~/Work/Tools/Crawlee/knowledge/sources'))
    assert cfg['remote'] == 'gdrive:memory-bank-backups'
    assert (cfg['local_retention_days'], cfg['remote_retention_days']) == (7, 30)     # the pre-R5 policy
    assert cfg['max_age_hours'] == 36 and cfg['command_timeout_s'] == 3600
    monkeypatch.setenv('SECOND_BRAIN_KB_SOURCES', '')
    assert backup.load_config(Path(__file__).resolve().parents[1] / 'config' / 'backup.toml')['kb_sources'] is None
    monkeypatch.setenv('SECOND_BRAIN_BACKUP_LOCAL_RETENTION_DAYS', '3')
    assert backup.load_config(Path(__file__).resolve().parents[1] / 'config' / 'backup.toml')['local_retention_days'] == 3


def test_cli_create_includes_the_configured_knowledge_sources_by_default(tmp_path, monkeypatch, capsys):
    """E-N3: 74ba02d omitted KB sources unless an env var nobody set was present."""
    key, repo = _key(tmp_path), _repo(tmp_path)
    kb = tmp_path / 'kb'
    kb.mkdir()
    (kb / 'source.md').write_text('knowledge')
    cfg = tmp_path / 'backup.toml'
    cfg.write_text(f'[bundle]\noutput = "{tmp_path / "out"}"\nkb_sources = "{kb}"\nkey = "{key}"\nlocal_retention_days = 0\nremote_retention_days = 0\n')
    for var in ('SECOND_BRAIN_KB_SOURCES', 'SECOND_BRAIN_BACKUP_REMOTE'):
        monkeypatch.delenv(var, raising=False)
    with patch.object(backup, 'command', side_effect=fake_dump):
        assert backup.main(['--config', str(cfg), 'create', '--repo', str(repo)]) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result['knowledge_sources_included'] is True and result['remote_verified'] is False
    assert 'knowledge-sources/source.md' in _members(result['archive'], key)
    with patch.object(backup, 'command', side_effect=fake_dump):
        assert backup.main(['--config', str(cfg), 'create', '--repo', str(repo), '--kb-sources', '']) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])['knowledge_sources_included'] is False


def test_configured_but_missing_knowledge_source_fails_instead_of_silently_omitting(tmp_path):
    key, repo = _key(tmp_path), _repo(tmp_path)
    with patch.object(backup, 'command', side_effect=fake_dump):
        with pytest.raises(ValueError, match='real directory'):
            backup.create_bundle(repo, tmp_path / 'out', key, kb=tmp_path / 'absent')
    assert not list((tmp_path / 'out').glob('*.gpg'))


# ------------------------------------------------------------- freshness / names

def test_parse_both_naming_schemes():
    created, kind = backup.parse_backup_name('second-brain_20261006T090102123456Z.tar.gpg')
    assert kind == 'bundle' and created == datetime(2026, 10, 6, 9, 1, 2, tzinfo=timezone.utc)
    created, kind = backup.parse_backup_name('memory_bank_20261005_020005.dump.gpg')
    assert kind == 'legacy_dump' and created.astimezone().strftime('%Y%m%d_%H%M%S') == '20261005_020005'
    assert backup.parse_backup_name('memories_20261005_020005.json.gpg') is None
    assert backup.parse_backup_name('kb-sources/') is None and backup.parse_backup_name('readme.txt') is None


def test_select_latest_picks_the_newest_across_schemes_and_judges_freshness():
    now = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
    listing = ['memory_bank_20261004_020005.dump.gpg', 'memories_20261004_020005.json.gpg',
               'second-brain_20261005T090000000000Z.tar.gpg', 'config/', 'kb-sources/']
    pick = backup.select_latest(listing, now=now, max_age_hours=36)
    assert pick['found'] and pick['name'] == 'second-brain_20261005T090000000000Z.tar.gpg' and pick['kind'] == 'bundle'
    assert pick['fresh'] is True and pick['age_hours'] == 18.0 and pick['candidates'] == 2
    stale = backup.select_latest(listing, now=now + timedelta(hours=30), max_age_hours=36)
    assert stale['found'] and stale['fresh'] is False and stale['age_hours'] == 48.0
    legacy_only = backup.select_latest(['memory_bank_20261005_020005.dump.gpg'], now=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).astimezone(), max_age_hours=36)
    assert legacy_only['kind'] == 'legacy_dump' and legacy_only['fresh'] is True
    assert backup.select_latest(['config/', ''], now=now)['found'] is False


def test_cli_select_latest_exit_codes(tmp_path, capsys):
    listing = tmp_path / 'list'
    recent = datetime.now(timezone.utc).strftime('second-brain_%Y%m%dT%H%M%S000000Z.tar.gpg')
    listing.write_text(f'{recent}\nmemory_bank_20260101_020005.dump.gpg\n')
    assert backup.main(['select-latest', str(listing)]) == 0
    assert json.loads(capsys.readouterr().out)['name'] == recent
    listing.write_text('memory_bank_20260101_020005.dump.gpg\n')
    assert backup.main(['select-latest', str(listing)]) == 1
    assert json.loads(capsys.readouterr().out)['fresh'] is False
    listing.write_text('nothing-encrypted.txt\n')
    assert backup.main(['select-latest', str(listing)]) == 1


def test_verify_legacy_dump_decrypts_and_lists_tables(tmp_path):
    key = _key(tmp_path)
    plain = tmp_path / 'legacy.dump'
    plain.write_bytes(b'synthetic legacy dump')
    archive = tmp_path / 'memory_bank_20261005_020005.dump.gpg'
    backup.gpg(['--symmetric', '--cipher-algo', 'AES256', '--output', archive, plain], key, tmp_path)
    plain.unlink()

    def listing(args, **kwargs):
        if args[0] == 'pg_restore':
            assert args[1] == '--list' and Path(args[2]).read_bytes() == b'synthetic legacy dump'
            return subprocess.CompletedProcess(args, 0, b'; Archive\n123; 0 0 TABLE DATA public memories\n124; 0 0 TABLE DATA public entities\n', b'')
        return _command(args, **kwargs)
    with patch.object(backup, 'command', side_effect=listing):
        result = backup.verify_legacy_dump(archive, key)
    assert result == {'archive': str(archive), 'format': 'legacy_dump', 'payload_verified': True, 'table_data_entries': 2,
                      'file_modes_verified': False, 'restore_verified': False}
    with patch.object(backup, 'command', side_effect=lambda a, **k: subprocess.CompletedProcess(a, 0, b'; nothing\n', b'') if a[0] == 'pg_restore' else _command(a, **k)):
        with pytest.raises(ValueError, match='TABLE DATA'):
            backup.verify_legacy_dump(archive, key)


# ------------------------------------------------------------------- hardening (ported)

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
            return super().communicate(timeout=0.05)

    expected = KeyboardInterrupt if interruption == 'keyboard' else subprocess.TimeoutExpired
    try:
        with patch.object(backup.subprocess, 'Popen', InterruptAfterLeaderExit):
            with pytest.raises(expected):
                backup.command([sys.executable, '-c', leader_code, grandchild_code, str(marker)])
        assert len(children) == 1
        child = children[0]
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
    key, repo = _key(tmp_path), _repo(tmp_path, 'retain me')
    with patch.object(backup, 'command', side_effect=fake_dump), patch.object(backup, 'gpg', side_effect=RuntimeError('encryption failed')):
        with pytest.raises(RuntimeError):
            backup.create_bundle(repo, tmp_path / 'out', key, remote='never:upload')
    assert (repo / 'README.md').read_text() == 'retain me'
    assert not list((tmp_path / 'out').glob('*.gpg'))


def test_payload_tampering_detected(tmp_path):
    (tmp_path / 'database.dump').write_bytes(b'before')
    (tmp_path / 'manifest.json').write_text(json.dumps({'version': 1, 'files': {'database.dump': {'bytes': 6, 'sha256': backup.digest(tmp_path / 'database.dump')}}}))
    (tmp_path / 'database.dump').write_bytes(b'after!')
    with pytest.raises(ValueError, match='checksum'):
        backup.validate_payload(tmp_path)


def test_symlink_secrets_are_excluded(tmp_path):
    secret = tmp_path / 'secret'
    secret.write_text('secret')
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'leak').symlink_to(secret)
    dest = tmp_path / 'copy'
    backup.copy_tree(repo, dest)
    assert not (dest / 'leak').exists()


def test_key_requires_private_permissions(tmp_path):
    key = tmp_path / 'key'
    key.write_bytes(b'synthetic test passphrase')
    key.chmod(0o644)
    with pytest.raises(ValueError, match='0600'):
        backup.check_key(key)


def test_configured_symlink_knowledge_root_fails(tmp_path):
    key, repo = _key(tmp_path), _repo(tmp_path)
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'source.md').write_text('knowledge')
    link = tmp_path / 'link'
    link.symlink_to(actual, target_is_directory=True)
    with patch.object(backup, 'command', side_effect=fake_dump):
        with pytest.raises(ValueError, match='real directory'):
            backup.create_bundle(repo, tmp_path / 'out', key, kb=link)
    assert not list((tmp_path / 'out').glob('*.gpg'))


def test_copy_preserves_owner_execution_without_public_permissions(tmp_path):
    source = tmp_path / 'job.sh'
    source.write_text('#!/bin/sh\nexit 0\n')
    source.chmod(0o755)
    target = tmp_path / 'restored' / 'job.sh'
    backup.copy_tree(source, target)
    assert target.stat().st_mode & 0o777 == 0o700


def test_manifest_rejects_lost_executable_permission(tmp_path):
    dump = tmp_path / 'database.dump'
    dump.write_bytes(b'dump')
    dump.chmod(0o600)
    script = tmp_path / 'script.sh'
    script.write_text('exit 0')
    script.chmod(0o600)
    files = {p.name: {'sha256': backup.digest(p), 'bytes': p.stat().st_size, 'mode': 0o700 if p == script else 0o600} for p in (dump, script)}
    (tmp_path / 'manifest.json').write_text(json.dumps({'version': 2, 'files': files}))
    with pytest.raises(ValueError, match='permissions'):
        backup.validate_payload(tmp_path)


# ------------------------------------------------- restore-check locale + diagnostics

def _drop_locale(monkeypatch):
    """Mimic launchd: no LANG and no LC_* in the environment at all."""
    for var in [v for v in os.environ if v == 'LANG' or v.startswith('LC_')]:
        monkeypatch.delenv(var, raising=False)


def _fake_cluster(calls, *, start_fails=False, fail_at=None):
    def run(args, **kwargs):
        argv = [str(a) for a in args]
        calls.append((argv, dict(kwargs.get('env') or {}), kwargs.get('diagnostics', True)))
        if argv[0] == fail_at:
            log = next(Path(c[0][c[0].index('-l') + 1]) for c in calls if c[0][0] == 'pg_ctl' and '-l' in c[0])
            with log.open('a') as f:
                f.write('ERROR:  duplicate key value\nCONTEXT:  COPY memories, line 1: "private row value"\n')
            raise RuntimeError(f'{fail_at} failed (exit 1)')
        if argv[0] == 'initdb':
            data = Path(argv[argv.index('-D') + 1])
            data.mkdir(parents=True)
            (data / 'postgresql.conf').write_text('')
        elif argv[0] == 'pg_ctl' and argv[-1] == 'start':
            Path(argv[argv.index('-l') + 1]).write_text(
                'LOG:  starting PostgreSQL 17\n'
                'FATAL:  postmaster became multithreaded during startup\n'
                'HINT:  Set the LC_ALL environment variable to a valid locale.\n')
            if start_fails:
                raise RuntimeError('pg_ctl failed (exit 1)')
        elif argv[0] == 'psql':
            return subprocess.CompletedProcess(args, 0, b'memories\n' if 'pg_tables' in argv[-1] else b'3\n', b'')
        return subprocess.CompletedProcess(args, 0, b'', b'')
    return run


@pytest.mark.parametrize('caller_locale', [None, 'en_US.UTF-8'])
def test_restore_check_pins_lc_all_c_regardless_of_the_callers_locale(tmp_path, monkeypatch, caller_locale):
    """Regression (verify 2026-10-11/13): launchd gives no LANG/LC_ALL, and PostgreSQL 17
    on macOS then aborts the restore cluster ("postmaster became multithreaded during
    startup"). Every cluster command — initdb, pg_ctl (and the postgres it forks),
    pg_restore, psql — must run with LC_ALL=C whatever the caller's environment holds."""
    _drop_locale(monkeypatch)
    if caller_locale:
        monkeypatch.setenv('LC_ALL', caller_locale)
        monkeypatch.setenv('LANG', caller_locale)
    calls = []
    with patch.object(backup, 'command', side_effect=_fake_cluster(calls)):
        result = backup.restore_check(tmp_path / 'db.dump', tmp_path, socket_dir=tmp_path / 'sock')
    assert result == {'restored': True, 'public_tables': 1, 'row_counts': {'memories': 3}}
    programs = [argv[0] for argv, _, _ in calls]
    assert {'initdb', 'pg_ctl', 'pg_restore', 'psql'} <= set(programs)
    for argv, env, _ in calls:
        assert env.get('LC_ALL') == 'C', argv
        assert not any(k.startswith('PG') and k not in ('PGHOST', 'PGPORT', 'PGUSER', 'PGDATABASE', 'PGPASSFILE') for k in env)
    restore = [d for argv, _, d in calls if argv[0] == 'pg_restore']
    assert restore == [False], 'pg_restore into the cluster can quote row values: stderr must be withheld'


def test_failed_pg_ctl_start_carries_the_cluster_log_tail(tmp_path, monkeypatch):
    """restore.log lives in verify's TemporaryDirectory and is deleted with it, so the
    server's FATAL/HINT must ride in the error itself."""
    _drop_locale(monkeypatch)
    calls = []
    with patch.object(backup, 'command', side_effect=_fake_cluster(calls, start_fails=True)):
        with pytest.raises(RuntimeError) as err:
            backup.restore_check(tmp_path / 'db.dump', tmp_path, socket_dir=tmp_path / 'sock')
    message = str(err.value)
    assert message.startswith('pg_ctl failed (exit 1)')
    assert 'restore.log tail:' in message and 'postmaster became multithreaded during startup' in message
    assert 'HINT:  Set the LC_ALL' in message
    assert not any(argv[0] in ('createdb', 'pg_restore') for argv, _, _ in calls)


@pytest.mark.parametrize('fail_at', ['createdb', 'pg_restore'])
def test_restore_log_is_tailed_only_when_pg_ctl_start_fails(tmp_path, monkeypatch, fail_at):
    """Once the cluster is up, restore.log can carry COPY CONTEXT lines quoting restored
    rows, so a failure in createdb or pg_restore must never echo any of it."""
    _drop_locale(monkeypatch)
    calls = []
    with patch.object(backup, 'command', side_effect=_fake_cluster(calls, fail_at=fail_at)):
        with pytest.raises(RuntimeError) as err:
            backup.restore_check(tmp_path / 'db.dump', tmp_path, socket_dir=tmp_path / 'sock')
    message = str(err.value)
    assert message == f'{fail_at} failed (exit 1)'
    assert (tmp_path / 'restore.log').read_text().count('private row value') == 1, 'fake must have written the log'
    for leaked in ('restore.log', 'private row value', 'CONTEXT', 'postmaster became multithreaded', 'HINT'):
        assert leaked not in message
    assert calls[-1][0][:2] == ['pg_ctl', '-D'] and calls[-1][0][-1] == 'stop'


def test_failing_command_error_carries_a_bounded_redacted_stderr_tail():
    code = textwrap.dedent('''
        import sys
        for i in range(50):
            print(f'diagnostic line {i:02d}', file=sys.stderr)
        print('gpg: passphrase hunter2 rejected', file=sys.stderr)
        print('final line', file=sys.stderr)
        sys.exit(3)
    ''')
    with pytest.raises(RuntimeError) as err:
        backup.command([sys.executable, '-c', code])
    message = str(err.value)
    assert message.startswith(Path(sys.executable).name + ' failed (exit 3); stderr tail:\n')
    tail = message.split('stderr tail:\n', 1)[1].splitlines()
    assert len(tail) == backup.STDERR_TAIL_LINES
    assert tail[-1] == 'final line' and tail[-2] == '[redacted line]' and tail[0] == 'diagnostic line 32'
    assert 'hunter2' not in message and 'diagnostic line 31' not in message

    flood = "import sys; sys.stderr.write('x' * 9000 + '\\n'); sys.exit(1)"
    with pytest.raises(RuntimeError) as err:
        backup.command([sys.executable, '-c', flood])
    assert len(str(err.value).encode()) < backup.STDERR_TAIL_BYTES + 200

    with pytest.raises(RuntimeError) as err:
        backup.command([sys.executable, '-c', "import sys; print('row value: private text', file=sys.stderr); sys.exit(2)"],
                       diagnostics=False)
    assert 'private text' not in str(err.value) and 'exit 2' in str(err.value)


@pytest.mark.parametrize('line', [
    'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def',
    'bearer abc123def456',
    'Cookie: session=abc123',
    'Set-Cookie: sid=abc123; HttpOnly',
    'failed to load credentials from ~/.aws',
    'X-Amz-Security-Token: IQoJb3JpZ2luX2VjEPr',
    'x-amz-date: 20261013T000000Z',
])
def test_diagnostic_tail_redacts_http_auth_cookie_credential_and_aws_header_lines(line):
    """rclone and pg_dump stderr reach the log via the default diagnostics=True, so
    HTTP auth headers, cookies, credential errors and signed AWS headers are withheld."""
    tail = backup.diagnostic_tail(f'harmless before\n{line}\nharmless after\n')
    assert tail.splitlines() == ['harmless before', '[redacted line]', 'harmless after']


# ------------------------------------------------- the real thing: dump → bundle → restore

def test_bundle_from_a_disposable_database_restores_into_another_with_identical_row_counts(test_conn, tmp_path, sample_memory_factory, monkeypatch):
    """Real pg_dump of the suite's disposable database, real gpg, real initdb +
    pg_restore into a second disposable cluster (socket inside the sandbox's permitted
    directory), then row counts compared table by table.  Runs with LANG/LC_* removed,
    as under launchd, where the cluster used to abort at startup."""
    _drop_locale(monkeypatch)
    for i in range(7):
        sample_memory_factory(title=f'restore roundtrip {i}', content=f'synthetic content {i}')
    with test_conn.cursor() as cur:
        cur.execute("INSERT INTO dream_cycle_runs (run_type, started_at, completed_at, candidates_generated) VALUES ('scheduled', now(), now(), 2)")
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
        tables = [r[0] for r in cur.fetchall()]
        expected = {}
        for table in tables:
            cur.execute(f'SELECT count(*) FROM public."{table}"')
            expected[table] = cur.fetchone()[0]
    assert expected['memories'] >= 7 and expected['dream_cycle_runs'] == 1 and expected['schema_migrations'] >= 15

    key, repo = _key(tmp_path), _repo(tmp_path)
    manifest = json.loads(Path(os.environ['SECOND_BRAIN_TEST_MANIFEST']).read_text())
    socket_dir = Path(manifest['host']) / f'restore-{os.getpid()}'
    output = tmp_path / 'backups'
    result = backup.create_bundle(repo, output, key)        # real pg_dump against DB_HOST/DB_NAME from the runner
    assert Path(result['archive']).exists() and result['payload_verified']

    verify_root = tmp_path / 'verify-root'       # this call's own scratch parent — other processes'
    verify_root.mkdir(mode=0o700)                # sb-verify-* dirs in /private/tmp are not ours to judge
    check = backup.verify_bundle(result['archive'], key, restore=True, socket_dir=socket_dir, temp_root=verify_root)
    assert check['restore_verified'] is True
    assert check['restore']['public_tables'] == len(tables)
    assert check['restore']['row_counts'] == expected
    print('\nrestore roundtrip row_counts (source == restored):', json.dumps(expected, sort_keys=True))
    assert not list(verify_root.iterdir()), 'temporary plaintext must be removed'
