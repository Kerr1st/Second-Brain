#!/usr/bin/env python3
"""Encrypted, receipted export of the knowledge-graph tables before migration 018 drops them.

``entities``, ``entity_edges`` and ``memory_entities`` (migration 008) leave the
schema in R7 (D4).  This script is the only path that lets 018 drop populated
tables: it writes a custom-format ``pg_dump`` of exactly those three tables,
encrypts it with the backup mechanism (GPG symmetric AES-256, the ``.backup-key``
passphrase file, same gpg options as scripts/maintenance/backup_bundle.py) to
``backups/graph-export/graph-export_<UTC stamp>.dump.gpg``, verifies it by
decrypting it again and listing it with ``pg_restore``, writes a receipt with the
per-table row counts and the sha256 of both the ciphertext and the plain dump, and
only THEN records the marker 018 looks for: one ``corpus_maintenance`` row
``(manifest_sha256 = <ciphertext sha256>, operation_key = 'graph-export',
details = {rows, archive, receipt, plain_sha256, exported_at})``.

Row counts are taken before and after the dump; if they differ a writer is still
running (the hourly qd-sync ``graph`` stage on the installed code) and the export
is refused -- stop the job first.  The archive lives in a *subdirectory* of
``backups/`` because the backup job's local retention deletes ``*.gpg`` directly
under ``backups/`` after 7 days; nothing prunes the subdirectory.  Copying the
ciphertext off this machine is the operator's step (R7 release notes §3).

Connects as the application role (DB_USER): SELECT on the three tables and INSERT
on corpus_maintenance are all it needs.  Never prints the key or a DSN.

Usage:
    .venv/bin/python scripts/maintenance/export_graph_tables.py --dry-run   # counts only
    .venv/bin/python scripts/maintenance/export_graph_tables.py             # export + marker
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.maintenance.backup_bundle import check_key, command, digest, gpg, load_config  # noqa: E402

TABLES = ('entities', 'entity_edges', 'memory_entities')
OPERATION_KEY = 'graph-export'
SUBDIR = 'graph-export'


def _connect():
    import psycopg2
    return psycopg2.connect(host=os.environ.get('DB_HOST', '127.0.0.1'), port=int(os.environ.get('DB_PORT', '5432')),
                            dbname=os.environ.get('DB_NAME', 'memory_bank'), user=os.environ.get('DB_USER', 'memory_bank'),
                            password=os.environ.get('DB_PASSWORD', ''), connect_timeout=5)


def row_counts(conn) -> dict:
    """Current row count per graph table; a missing table counts as absent (None)."""
    counts = {}
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute('SELECT to_regclass(%s)', (f'public.{table}',))
            if cur.fetchone()[0] is None:
                counts[table] = None
                continue
            cur.execute(f'SELECT count(*) FROM public."{table}"')
            counts[table] = cur.fetchone()[0]
    return counts


def export(output, key, *, conn=None) -> dict:
    key = check_key(key)
    output = Path(output).resolve() / SUBDIR
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    own = conn is None
    conn = conn or _connect()
    try:
        before = row_counts(conn)
        present = [t for t in TABLES if before[t] is not None]
        if not present:
            raise ValueError('nothing to export: the knowledge-graph tables are already gone')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target = output / f'graph-export_{stamp}.dump.gpg'
        receipt = output / f'graph-export_{stamp}.receipt.json'
        if target.exists() or receipt.exists():
            raise ValueError(f'{target.name} already exists; wait a second and retry')
        with tempfile.TemporaryDirectory(prefix='.graph-export-', dir=output) as temp:
            area = Path(temp)
            plain = area / 'graph.dump'
            db_env = dict(os.environ)
            db_env['PGPASSWORD'] = os.environ.get('DB_PASSWORD', os.environ.get('PGPASSWORD', ''))
            command(['pg_dump', '-h', os.environ.get('DB_HOST', '127.0.0.1'), '-p', os.environ.get('DB_PORT', '5432'),
                     '-U', os.environ.get('DB_USER', 'memory_bank'), '-d', os.environ.get('DB_NAME', 'memory_bank'),
                     '--format=custom', '--no-owner', '--no-acl', *[a for t in present for a in ('--table', f'public.{t}')],
                     '--file', plain], env=db_env)
            plain.chmod(0o600)
            after = row_counts(conn)
            if after != before:
                raise ValueError(f'knowledge-graph tables changed during the export ({before} -> {after}); stop the writer (qd-sync) and retry')
            plain_sha = digest(plain)
            partial = area / 'encrypted.gpg'
            gpg(['--symmetric', '--cipher-algo', 'AES256', '--output', partial, plain], key, area)
            # Verify what will be kept: decrypt the ciphertext again, compare, list.
            check = area / 'check.dump'
            gpg(['--output', check, '--decrypt', partial], key, area)
            if digest(check) != plain_sha:
                raise ValueError('decrypted archive differs from the dump')
            listing = command(['pg_restore', '--list', check]).stdout.decode(errors='replace')
            missing = [t for t in present if not re.search(rf'TABLE DATA public {t}\b', listing)]
            if missing:
                raise ValueError(f'archive lists no TABLE DATA for {missing}')
            partial.replace(target)
            target.chmod(0o600)
        rows = {t: before[t] for t in present}
        record = {'archive': str(target), 'receipt': str(receipt), 'sha256': digest(target), 'bytes': target.stat().st_size,
                  'plain_sha256': plain_sha, 'rows': rows, 'tables': present, 'exported_at': stamp,
                  'database': os.environ.get('DB_NAME', 'memory_bank'), 'verified': True}
        receipt.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
        receipt.chmod(0o600)
        # The marker 018 requires -- written last, after archive and receipt exist.
        with conn.cursor() as cur:
            cur.execute('INSERT INTO corpus_maintenance (manifest_sha256, operation_key, details) VALUES (%s, %s, %s::jsonb)',
                        (record['sha256'], OPERATION_KEY,
                         json.dumps({'rows': rows, 'archive': str(target), 'receipt': str(receipt),
                                     'plain_sha256': plain_sha, 'exported_at': stamp})))
        conn.commit()
        record['marker'] = {'operation_key': OPERATION_KEY, 'manifest_sha256': record['sha256']}
        return record
    finally:
        if own:
            conn.close()


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, default=None, help='backup policy (default config/backup.toml; output + key)')
    parser.add_argument('--output', type=Path, default=None, help='backups directory (default from config, relative to the repo)')
    parser.add_argument('--key', type=Path, default=None, help='passphrase file (default from config)')
    parser.add_argument('--dry-run', action='store_true', help='print the row counts only; write nothing')
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        output = args.output if args.output is not None else (cfg['output'] if cfg['output'].is_absolute() else ROOT / cfg['output'])
        key = args.key or (cfg['key'] if cfg['key'].is_absolute() else ROOT / cfg['key'])
        if args.dry_run:
            conn = _connect()
            try:
                counts = row_counts(conn)
            finally:
                conn.close()
            print(json.dumps({'dry_run': True, 'rows': counts, 'output': str(Path(output) / SUBDIR)}, sort_keys=True))
            return 0
        result = export(output, key)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'status': 'failed', 'reason': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
