#!/usr/bin/env python3
"""Encrypted database/source/config bundles with checksums and isolated restore.

No service starts, schema edits or plaintext cloud transfers. Cloud success means
an uploaded ciphertext was downloaded again and matched byte-for-byte. Restore
verification always creates a new socket-only cluster, never an existing target.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
INCLUDES = ('src', 'scripts', 'docs', 'config', 'migrations', 'scheduling', '.kiro',
            '.env', 'README.md', 'requirements.txt', 'requirements-dev.txt',
            'requirements.lock', 'docker-compose.yml', 'pyproject.toml')
EXCLUDE = {'.git', '.venv', '__pycache__', 'logs', 'backups', '.backup-key', '.dream'}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def command(args, **kwargs):
    """Keep subprocess diagnostics private; never print a DSN or decrypted data."""
    child = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, **kwargs)
    try:
        stdout, stderr = child.communicate(timeout=1800)
    except BaseException:
        # The leader can exit while a grandchild still owns its pipes. Always
        # signal the owned process group, then escalate after a bounded wait.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=5)
        raise
    if child.returncode:
        raise RuntimeError(f'{Path(str(args[0])).name} failed (exit {child.returncode})')
    return subprocess.CompletedProcess(args, child.returncode, stdout, stderr)


def check_key(key):
    key = Path(key)
    if not key.is_file() or key.is_symlink() or key.stat().st_size < 16:
        raise ValueError('A regular recovery key file of at least 16 bytes is required')
    if key.stat().st_mode & 0o077:
        raise ValueError('Recovery key permissions must be 0600 or stricter')
    return key


def gpg(args, key, area):
    home = Path(area) / 'gnupg'
    home.mkdir(mode=0o700, exist_ok=True)
    return command(['gpg', '--homedir', home, '--batch', '--yes', '--no-autostart', '--no-symkey-cache',
                    '--s2k-mode', '3', '--s2k-count', '65011712', '--s2k-digest-algo', 'SHA256',
                    '--pinentry-mode', 'loopback', '--passphrase-file', key, *args])


def copy_tree(source, destination):
    """Copy regular files only; never follow links into another private surface."""
    source = Path(source)
    if source.is_symlink():
        return
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o600 | (source.stat().st_mode & 0o100))
    elif source.is_dir():
        for child in sorted(source.iterdir()):
            if child.name not in EXCLUDE and not child.is_symlink():
                copy_tree(child, destination / child.name)


def validate_payload(root):
    root = Path(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('version') not in (1, 2) or 'database.dump' not in manifest.get('files', {}):
        raise ValueError('Unsupported or incomplete backup manifest')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p != root / 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('Backup member inventory differs from manifest')
    for name, expected in manifest['files'].items():
        p = root / name
        if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink() or digest(p) != expected['sha256'] or p.stat().st_size != expected['bytes']:
            raise ValueError('Backup payload checksum mismatch')
        if manifest['version'] >= 2 and (expected.get('mode') not in (0o600,0o700) or p.stat().st_mode & 0o777 != expected['mode']):
            raise ValueError('Backup executable permissions differ from manifest')
    command(['pg_restore', '--list', root / 'database.dump'])
    return manifest


def extract_bundle(archive, key, area):
    tar = Path(area) / 'decrypted.tar'
    gpg(['--output', tar, '--decrypt', archive], key, area)
    root = Path(area) / 'payload'
    root.mkdir(mode=0o700)
    with tarfile.open(tar, 'r') as reader:
        names = set()
        for member in reader.getmembers():
            if not member.isfile() and not member.isdir():
                raise ValueError('Non-regular archive entry refused')
            if Path(member.name).is_absolute() or '..' in Path(member.name).parts or member.name in names:
                raise ValueError('Unsafe or duplicate archive member refused')
            names.add(member.name)
        reader.extractall(root, filter='data')
    return root, validate_payload(root)


def restore_check(dump, area):
    """Restore the entire dump into an isolated, no-TCP PostgreSQL cluster."""
    area = Path(area)
    data, sockets = area / 'pg-data', area / 'socket'
    sockets.mkdir(mode=0o700)
    pg_env = dict(os.environ)
    for key in tuple(pg_env):
        if key.startswith('PG') or key.startswith('DB_'):
            pg_env.pop(key)
    pg_env.update(PGHOST=str(sockets), PGPORT='5432', PGUSER='sb_restore', PGDATABASE='postgres', PGPASSFILE=str(area / 'absent-pgpass'))
    started = False
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        command(['initdb', '-D', data, '-U', 'sb_restore', '--auth-local=trust', '--auth-host=reject', '--no-locale', '-E', 'UTF8'], env=pg_env)
        with (data / 'postgresql.conf').open('a') as f:
            f.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{sockets}'\nmaintenance_work_mem = '512MB'\n")
        command(['pg_ctl', '-D', data, '-l', area / 'restore.log', '-w', 'start'], env=pg_env)
        started = True
        command(['createdb', 'sb_restore_check'], env=pg_env)
        command(['pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-d', 'sb_restore_check', dump], env=pg_env)
        result = command(['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-d', 'sb_restore_check', '-Atc',
                          "SELECT count(*) FROM pg_tables WHERE schemaname='public'"], env=pg_env)
        return {'restored': True, 'public_tables': int(result.stdout.strip())}
    finally:
        try:
            if started or (data / 'postmaster.pid').exists():
                command(['pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop'], env=pg_env)
        finally:
            signal.signal(signal.SIGTERM, previous)


def create_bundle(repo, output, key, kb=None, remote=None):
    key = check_key(key)
    repo, output = Path(repo).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = output / f'second-brain_{stamp}.tar.gpg'
    with tempfile.TemporaryDirectory(prefix='.sb-backup-', dir=output) as temp:
        area = Path(temp)
        payload = area / 'payload'
        payload.mkdir(mode=0o700)
        db_env = dict(os.environ)
        db_env['PGPASSWORD'] = os.environ.get('DB_PASSWORD', os.environ.get('PGPASSWORD', ''))
        command(['pg_dump', '-h', os.environ.get('DB_HOST', '127.0.0.1'), '-p', os.environ.get('DB_PORT', '5432'),
                 '-U', os.environ.get('DB_USER', 'memory_bank'), '-d', os.environ.get('DB_NAME', 'memory_bank'),
                 '--format=custom', '--no-owner', '--no-acl', '--file', payload / 'database.dump'], env=db_env)
        (payload / 'database.dump').chmod(0o600)
        for name in INCLUDES:
            copy_tree(repo / name, payload / 'repository' / name)
        if kb:
            if Path(kb).is_symlink() or not Path(kb).is_dir():
                raise ValueError('Configured knowledge-source directory must be a real directory, not a symlink')
            copy_tree(Path(kb), payload / 'knowledge-sources')
        files = {str(p.relative_to(payload)): {'bytes': p.stat().st_size, 'sha256': digest(p), 'mode': p.stat().st_mode & 0o777}
                 for p in sorted(payload.rglob('*')) if p.is_file()}
        manifest = {'version': 2, 'created_at': stamp, 'files': files, 'knowledge_sources_included': bool(kb)}
        (payload / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        validate_payload(payload)
        tar = area / 'payload.tar'
        with tarfile.open(tar, 'w') as archive:
            for child in sorted(payload.iterdir()):
                archive.add(child, arcname=child.name)
        partial = area / 'encrypted.gpg'
        gpg(['--symmetric', '--cipher-algo', 'AES256', '--output', partial, tar], key, area)
        verify_area = area / 'verify'
        verify_area.mkdir(mode=0o700)
        extract_bundle(partial, key, verify_area)
        partial.replace(target)
        target.chmod(0o600)
        remote_verified = False
        if remote:
            remote_file = remote.rstrip('/') + '/' + target.name
            command(['rclone', 'copyto', target, remote_file])
            downloaded = area / 'downloaded.gpg'
            command(['rclone', 'copyto', remote_file, downloaded])
            if digest(downloaded) != digest(target):
                raise ValueError('Cloud ciphertext differs from uploaded bundle')
            remote_verified = True
        record = {'archive': str(target), 'sha256': digest(target), 'bytes': target.stat().st_size,
                  'payload_verified': True, 'file_modes_verified': True, 'restore_verified': False, 'remote_verified': remote_verified}
        target.with_suffix('.receipt.json').write_text(json.dumps(record, indent=2) + '\n')
        return record


def verify_bundle(archive, key, restore=False):
    key = check_key(key)
    with tempfile.TemporaryDirectory(prefix='sb-verify-', dir='/private/tmp' if Path('/private/tmp').exists() else None) as temp:
        root, manifest = extract_bundle(Path(archive).resolve(), key, temp)
        result = {'archive': str(archive), 'payload_verified': True, 'files': len(manifest['files']), 'file_modes_verified': manifest['version'] >= 2, 'restore_verified': False}
        if restore:
            result['restore'] = restore_check(root / 'database.dump', temp)
            result['restore_verified'] = True
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    create = sub.add_parser('create')
    create.add_argument('--repo', type=Path, default=ROOT)
    create.add_argument('--output', type=Path, default=ROOT / 'backups')
    create.add_argument('--kb-sources', type=Path)
    create.add_argument('--remote', help='Only encrypted bundle is uploaded; no sync/delete')
    verify = sub.add_parser('verify')
    verify.add_argument('archive', type=Path)
    verify.add_argument('--restore', action='store_true')
    for p in (create, verify):
        p.add_argument('--key', type=Path, default=Path(os.environ.get('SECOND_BRAIN_BACKUP_KEY', ROOT / '.backup-key')))
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        result = (create_bundle(args.repo, args.output, args.key, args.kb_sources, args.remote) if args.mode == 'create'
                  else verify_bundle(args.archive, args.key, args.restore))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(json.dumps({'status': 'failed', 'reason': str(exc)}))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({'status': 'interrupted'}))
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
