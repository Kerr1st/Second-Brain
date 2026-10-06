#!/usr/bin/env python3
"""Encrypted database/source/config bundles with checksums, retention and isolated restore.

One bundle per run: ``backups/second-brain_<UTC stamp>.tar.gpg`` (AES-256, GPG
symmetric, passphrase file ``.backup-key``) holding a custom-format ``pg_dump``,
the repository's tracked trees (``src scripts docs config migrations scheduling
.kiro`` …), the gitignored ``.env`` and the knowledge-source directory named in
``config/backup.toml``.  ``.env`` (CAPTURE_API_TOKEN) travels inside the bundle on
purpose — a replacement host needs it — and that is acceptable ONLY because the
bundle is ciphertext end to end: plaintext exists in a mode-0700 temporary
directory under ``backups/`` for the duration of the run and is removed before
the ``.tar.gpg`` is published.  Nothing else is ever handed to rclone.

No service starts, schema edits or plaintext cloud transfers.  Cloud success means
an uploaded ciphertext was downloaded again and matched byte-for-byte.  Retention
(local N days, remote M days, ``*.gpg`` only) runs only after that verification.
Restore verification always creates a new socket-only cluster, never an existing
target, and reports per-table row counts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / 'config' / 'backup.toml'
INCLUDES = ('src', 'scripts', 'docs', 'config', 'migrations', 'scheduling', '.kiro',
            '.env', 'README.md', 'requirements.txt', 'requirements-dev.txt',
            'requirements.lock', 'docker-compose.yml', 'pyproject.toml')
EXCLUDE = {'.git', '.venv', '__pycache__', 'logs', 'backups', '.backup-key', '.dream', 'node_modules', '.hypothesis'}
COMMAND_TIMEOUT_S = 3600

# Both encrypted-file naming schemes that may coexist on the remote during the
# transition from the pre-R5 backup.sh (memory_bank_<ts>.dump.gpg) to bundles.
BUNDLE_NAME = re.compile(r'^second-brain_(\d{8}T\d{6})(\d{6})?Z\.tar\.gpg$')
BUNDLE_SUFFIX = '.tar.gpg'
RECEIPT_SUFFIX = '.receipt.json'


def receipt_path(archive) -> Path:
    """``second-brain_<stamp>.tar.gpg`` → ``second-brain_<stamp>.receipt.json`` (the
    documented name; ``Path.with_suffix`` would keep the ``.tar`` and produce
    ``…tar.receipt.json``)."""
    archive = Path(archive)
    if not archive.name.endswith(BUNDLE_SUFFIX):
        raise ValueError(f'not a bundle name: {archive.name}')
    return archive.with_name(archive.name[:-len(BUNDLE_SUFFIX)] + RECEIPT_SUFFIX)
LEGACY_NAME = re.compile(r'^memory_bank_(\d{8})_(\d{6})\.dump\.gpg$')


# ------------------------------------------------------------------ config

def _env_or(cfg, key, env_name, default):
    if env_name in os.environ:
        return os.environ[env_name]
    return cfg.get(key, default)


def load_config(path=None) -> dict:
    path = Path(path or os.environ.get('SECOND_BRAIN_BACKUP_CONFIG', DEFAULT_CONFIG))
    raw = tomllib.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    bundle, verify = raw.get('bundle', {}), raw.get('verify', {})
    kb = _env_or(bundle, 'kb_sources', 'SECOND_BRAIN_KB_SOURCES', None)
    remote = _env_or(bundle, 'remote', 'SECOND_BRAIN_BACKUP_REMOTE', None)
    cfg = {
        'output': Path(str(bundle.get('output', 'backups'))),
        'remote': remote or None,
        'key': Path(os.path.expanduser(str(_env_or(bundle, 'key', 'SECOND_BRAIN_BACKUP_KEY', '.backup-key')))),
        'kb_sources': Path(os.path.expanduser(str(kb))) if kb else None,
        'local_retention_days': int(_env_or(bundle, 'local_retention_days', 'SECOND_BRAIN_BACKUP_LOCAL_RETENTION_DAYS', 7)),
        'remote_retention_days': int(_env_or(bundle, 'remote_retention_days', 'SECOND_BRAIN_BACKUP_REMOTE_RETENTION_DAYS', 30)),
        'command_timeout_s': int(_env_or(bundle, 'command_timeout_s', 'SECOND_BRAIN_BACKUP_COMMAND_TIMEOUT_S', 3600)),
        'max_age_hours': float(_env_or(verify, 'max_age_hours', 'SECOND_BRAIN_BACKUP_MAX_AGE_HOURS', 36)),
    }
    for name in ('local_retention_days', 'remote_retention_days'):
        if cfg[name] < 0:
            raise ValueError(f'{name} must be >= 0 (0 disables)')
    if cfg['command_timeout_s'] <= 0 or cfg['max_age_hours'] <= 0:
        raise ValueError('command_timeout_s and max_age_hours must be positive')
    return cfg


# ------------------------------------------------------------- primitives

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
        stdout, stderr = child.communicate(timeout=COMMAND_TIMEOUT_S)
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
        if manifest['version'] >= 2 and (expected.get('mode') not in (0o600, 0o700) or p.stat().st_mode & 0o777 != expected['mode']):
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


# ---------------------------------------------------------------- restore

def restore_check(dump, area, *, socket_dir=None, port='5432'):
    """Restore the entire dump into an isolated, no-TCP PostgreSQL cluster and
    return per-table row counts.  ``socket_dir`` defaults to a private directory
    under ``area`` (tests pass their sandbox's permitted socket directory)."""
    area = Path(area)
    data = area / 'pg-data'
    sockets = Path(socket_dir or os.environ.get('SECOND_BRAIN_RESTORE_SOCKET_DIR') or (area / 'socket'))
    sockets.mkdir(mode=0o700, parents=True, exist_ok=True)
    pg_env = dict(os.environ)
    for key in tuple(pg_env):
        if key.startswith('PG') or key.startswith('DB_'):
            pg_env.pop(key)
    pg_env.update(PGHOST=str(sockets), PGPORT=str(port), PGUSER='sb_restore', PGDATABASE='postgres',
                  PGPASSFILE=str(area / 'absent-pgpass'))
    started = False

    def interrupted(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        command(['initdb', '-D', data, '-U', 'sb_restore', '--auth-local=trust', '--auth-host=reject', '--no-locale', '-E', 'UTF8'], env=pg_env)
        with (data / 'postgresql.conf').open('a') as f:
            f.write(f"\nlisten_addresses = ''\nport = {port}\nunix_socket_directories = '{sockets}'\nmaintenance_work_mem = '512MB'\n")
        command(['pg_ctl', '-D', data, '-l', area / 'restore.log', '-w', 'start'], env=pg_env)
        started = True
        command(['createdb', 'sb_restore_check'], env=pg_env)
        command(['pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-d', 'sb_restore_check', dump], env=pg_env)
        psql = ['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-d', 'sb_restore_check', '-Atc']
        tables = command(psql + ["SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"], env=pg_env).stdout.decode().split()
        counts = {}
        for table in tables:
            if not re.fullmatch(r'[a-z_][a-z0-9_]*', table):
                raise ValueError('Unexpected table name in restored dump')
            counts[table] = int(command(psql + [f'SELECT count(*) FROM public."{table}"'], env=pg_env).stdout.strip())
        return {'restored': True, 'public_tables': len(tables), 'row_counts': counts}
    finally:
        try:
            if started or (data / 'postmaster.pid').exists():
                command(['pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop'], env=pg_env)
        finally:
            signal.signal(signal.SIGTERM, previous)


# -------------------------------------------------------------- retention

def prune_local(output, days, *, now=None, keep=()) -> list[str]:
    """Delete ``*.gpg`` and ``*.receipt.json`` directly under ``output`` whose mtime is
    older than ``days`` (both bundle and legacy names; never subdirectories, never
    the files in ``keep``).  ``days == 0`` disables."""
    if not days:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).timestamp()
    keep = {Path(k).resolve() for k in keep}
    removed = []
    for pattern in ('*.gpg', '*.receipt.json'):
        for path in sorted(Path(output).glob(pattern)):
            if path.is_file() and not path.is_symlink() and path.resolve() not in keep and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.name)
    return removed


def prune_remote(remote, days) -> list:
    """``rclone delete <remote> --min-age <N>d --include *.gpg --max-depth 1``: only
    ciphertext at the root of the remote; the historical plaintext trees are never
    in scope.  ``days == 0`` disables."""
    if not days:
        return []
    args = ['rclone', 'delete', remote.rstrip('/') + '/', '--min-age', f'{int(days)}d', '--include', '*.gpg', '--max-depth', '1']
    command(args)
    return args


# ----------------------------------------------------------------- create

def create_bundle(repo, output, key, kb=None, remote=None, *, local_retention_days=0, remote_retention_days=0):
    key = check_key(key)
    repo, output = Path(repo).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = output / f'second-brain_{stamp}.tar.gpg'
    if kb is not None:
        kb = Path(kb)
        if kb.is_symlink() or not kb.is_dir():
            raise ValueError('Configured knowledge-source directory must be a real directory, not a symlink')
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
        if kb is not None:
            copy_tree(kb, payload / 'knowledge-sources')
        files = {str(p.relative_to(payload)): {'bytes': p.stat().st_size, 'sha256': digest(p), 'mode': p.stat().st_mode & 0o777}
                 for p in sorted(payload.rglob('*')) if p.is_file()}
        manifest = {'version': 2, 'created_at': stamp, 'files': files, 'knowledge_sources_included': kb is not None,
                    'knowledge_sources_path': str(kb) if kb is not None else None}
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
                  'payload_verified': True, 'file_modes_verified': True, 'restore_verified': False,
                  'remote_verified': remote_verified, 'knowledge_sources_included': kb is not None,
                  'files': len(files)}
        # Retention only once this bundle is itself verified (locally; and on the
        # remote only when the read-back matched) — a failed upload prunes nothing remote.
        receipt = receipt_path(target)
        record['receipt'] = str(receipt)
        record['pruned_local'] = prune_local(output, local_retention_days, keep=[target, receipt])
        record['pruned_remote'] = bool(remote_verified and remote_retention_days and prune_remote(remote, remote_retention_days))
        record['retention'] = {'local_days': local_retention_days, 'remote_days': remote_retention_days}
        receipt.write_text(json.dumps(record, indent=2) + '\n')
        receipt.chmod(0o600)
        return record


# ----------------------------------------------------------------- verify

def verify_bundle(archive, key, restore=False, *, socket_dir=None):
    key = check_key(key)
    with tempfile.TemporaryDirectory(prefix='sb-verify-', dir='/private/tmp' if Path('/private/tmp').exists() else None) as temp:
        root, manifest = extract_bundle(Path(archive).resolve(), key, temp)
        result = {'archive': str(archive), 'format': 'bundle', 'payload_verified': True, 'files': len(manifest['files']),
                  'file_modes_verified': manifest['version'] >= 2, 'restore_verified': False,
                  'knowledge_sources_included': bool(manifest.get('knowledge_sources_included'))}
        if restore:
            result['restore'] = restore_check(root / 'database.dump', temp, socket_dir=socket_dir)
            result['restore_verified'] = True
        return result


def verify_legacy_dump(archive, key):
    """Pre-R5 format (memory_bank_<ts>.dump.gpg): decrypt and list the custom dump."""
    key = check_key(key)
    with tempfile.TemporaryDirectory(prefix='sb-verify-legacy-', dir='/private/tmp' if Path('/private/tmp').exists() else None) as temp:
        dump = Path(temp) / 'legacy.dump'
        gpg(['--output', dump, '--decrypt', Path(archive).resolve()], key, temp)
        listing = command(['pg_restore', '--list', dump]).stdout.decode(errors='replace')
        tables = sum(1 for line in listing.splitlines() if 'TABLE DATA' in line)
        if tables < 1:
            raise ValueError('Legacy dump lists no TABLE DATA entries')
        return {'archive': str(archive), 'format': 'legacy_dump', 'payload_verified': True, 'table_data_entries': tables,
                'file_modes_verified': False, 'restore_verified': False}


def parse_backup_name(name: str):
    """(created_at UTC, kind) for either naming scheme, else None."""
    m = BUNDLE_NAME.match(name)
    if m:
        return datetime.strptime(m.group(1), '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc), 'bundle'
    m = LEGACY_NAME.match(name)
    if m:
        # Legacy stamps are the Mac's local wall clock (`date +%Y%m%d_%H%M%S`).
        local = datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
        return local.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc), 'legacy_dump'
    return None


def select_latest(names, *, now=None, max_age_hours=36.0) -> dict:
    """Newest encrypted backup among ``names`` (either scheme) with a freshness verdict."""
    now = now or datetime.now(timezone.utc)
    candidates = []
    for raw in names:
        name = raw.strip()
        if not name or '/' in name:
            continue
        parsed = parse_backup_name(name)
        if parsed:
            candidates.append((parsed[0], name, parsed[1]))
    if not candidates:
        return {'found': False, 'fresh': False, 'reason': 'no encrypted backup of either naming scheme'}
    created, name, kind = max(candidates)
    age_h = (now - created).total_seconds() / 3600
    return {'found': True, 'name': name, 'kind': kind, 'created_at': created.isoformat(), 'age_hours': round(age_h, 2),
            'max_age_hours': max_age_hours, 'fresh': age_h <= max_age_hours, 'candidates': len(candidates)}


# ------------------------------------------------------------------- main

def main(argv=None):
    global COMMAND_TIMEOUT_S
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, default=None, help='backup policy (default config/backup.toml)')
    sub = parser.add_subparsers(dest='mode', required=True)
    create = sub.add_parser('create')
    create.add_argument('--repo', type=Path, default=ROOT)
    create.add_argument('--output', type=Path, default=None)
    create.add_argument('--kb-sources', default=None, help='override config; "" excludes')
    create.add_argument('--no-kb-sources', action='store_true')
    create.add_argument('--remote', default=None, help='override config; only the encrypted bundle is uploaded')
    create.add_argument('--local-retention-days', type=int, default=None)
    create.add_argument('--remote-retention-days', type=int, default=None)
    verify = sub.add_parser('verify')
    verify.add_argument('archive', type=Path)
    verify.add_argument('--restore', action='store_true')
    legacy = sub.add_parser('verify-legacy')
    legacy.add_argument('archive', type=Path)
    latest = sub.add_parser('select-latest', help='pick the newest encrypted backup from an rclone lsf listing')
    latest.add_argument('listing', type=Path, help='file with one remote file name per line')
    latest.add_argument('--max-age-hours', type=float, default=None)
    for p in (create, verify, legacy):
        p.add_argument('--key', type=Path, default=None)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    COMMAND_TIMEOUT_S = cfg['command_timeout_s']

    def interrupted(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.mode == 'create':
            repo = args.repo.resolve()
            output = args.output if args.output is not None else (cfg['output'] if cfg['output'].is_absolute() else repo / cfg['output'])
            key = args.key or (cfg['key'] if cfg['key'].is_absolute() else repo / cfg['key'])
            if args.no_kb_sources:
                kb = None
            elif args.kb_sources is None:
                kb = cfg['kb_sources']
            else:
                kb = Path(os.path.expanduser(args.kb_sources)) if args.kb_sources.strip() else None
            remote = args.remote if args.remote is not None else cfg['remote']
            result = create_bundle(repo, output, key, kb, remote or None,
                                   local_retention_days=cfg['local_retention_days'] if args.local_retention_days is None else args.local_retention_days,
                                   remote_retention_days=cfg['remote_retention_days'] if args.remote_retention_days is None else args.remote_retention_days)
        elif args.mode == 'verify':
            key = args.key or (cfg['key'] if cfg['key'].is_absolute() else ROOT / cfg['key'])
            result = verify_bundle(args.archive, key, args.restore)
        elif args.mode == 'verify-legacy':
            key = args.key or (cfg['key'] if cfg['key'].is_absolute() else ROOT / cfg['key'])
            result = verify_legacy_dump(args.archive, key)
        else:
            result = select_latest(args.listing.read_text(encoding='utf-8').splitlines(),
                                   max_age_hours=cfg['max_age_hours'] if args.max_age_hours is None else args.max_age_hours)
            print(json.dumps(result, sort_keys=True))
            return 0 if result['found'] and result['fresh'] else 1
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
