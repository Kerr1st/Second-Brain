#!/usr/bin/env python3
"""Inventory retirement candidates without deleting, relinking or changing credentials.

The private manifest binds reviewed file versions and distinguishes eligible
identical plist copies from divergent installations. Graph inspection is opt-in
and read-only. No apply mode exists: deployment, revocation and deletion require
an independently verified recovery bundle and an explicitly targeted release.
"""
from __future__ import annotations
import argparse
import configparser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RETIREMENT_PATHS = (
    'scripts/backups', 'scripts/logs', 'src/logs/llm_metrics',
    'tests/fixtures/golden_run', 'logs/llm_metrics/adhoc.jsonl',
)
GRAPH_TABLES = ('memory_entities', 'entity_edges', 'entities')


def fingerprint(path):
    """Hash one regular file, rejecting a changing source and never following links."""
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('A regular file is required for a content fingerprint')
    h = hashlib.sha256()
    # O_NOFOLLOW prevents swapping a previously inspected file for a symlink.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
        finished = os.fstat(handle.fileno())
    after = path.lstat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if not signature(before) == signature(opened) == signature(finished) == signature(after):
        raise ValueError('Source changed during read-only inventory; retry after it is stable')
    return {'bytes': before.st_size, 'sha256': h.hexdigest(),
            'mode': stat.S_IMODE(before.st_mode), 'mtime_ns': before.st_mtime_ns}


def file_inventory(root):
    """Record private file identities; symlinks are visible but never traversed."""
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        return {'state': 'missing', 'files': [], 'symlinks': [], 'bytes': 0}
    if root.is_symlink():
        return {'state': 'symlink_not_followed', 'files': [], 'symlinks': ['.'], 'bytes': 0}
    files, links = [], []
    if root.is_file():
        files.append({'path': '.', **fingerprint(root)})
    else:
        def inaccessible(error):
            raise error
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=inaccessible):
            directory = Path(directory)
            for name in sorted(dirs + names):
                path = directory / name
                if path.is_symlink():
                    links.append(str(path.relative_to(root)))
                elif path.is_file():
                    files.append({'path': str(path.relative_to(root)), **fingerprint(path)})
            dirs[:] = sorted(name for name in dirs if not (directory / name).is_symlink())
    return {'state': 'present', 'files': sorted(files, key=lambda item: item['path']),
            'symlinks': sorted(links), 'bytes': sum(item['bytes'] for item in files)}


def plist_inventory(repo, installed):
    results = []
    for target in sorted((Path(repo) / 'scheduling').glob('com.second-brain.*.plist')):
        source = Path(installed) / target.name
        template = fingerprint(target)
        with target.open('rb') as handle:
            label = plistlib.load(handle)['Label']
        result = {'label': label, 'installed_path': str(source), 'target_path': str(target),
                  'target_version': template, 'changes_loaded_state': False}
        if source.is_symlink():
            result['state'] = 'linked_to_target' if source.resolve() == target.resolve() else 'different_symlink'
            result['action'] = 'none' if result['state'] == 'linked_to_target' else 'review_target'
        elif not source.exists():
            result.update(state='not_installed', action='preserve_absence')
        elif not source.is_file():
            result.update(state='unsupported_installed_type', action='review')
        else:
            result['installed_version'] = fingerprint(source)
            equal = result['installed_version']['sha256'] == template['sha256']
            result.update(state='identical_copy' if equal else 'divergent_copy',
                          action='preserve_copy_then_replace_with_symlink' if equal else 'review_diff_before_deployment')
        results.append(result)
    return results


def credential_inventory(credentials, config):
    """Only profile names, mechanism presence and file metadata; never secret values."""
    result = {'files': [], 'profiles': [], 'replacement_ready': False,
              'remaining': ['Choose and provision an unattended temporary-credential provider',
                            'Verify expiry renewal, least privilege and synthetic model access',
                            'Identify the old IAM key and revoke only after replacement succeeds']}
    profiles = {}
    for kind, path in [('credentials', Path(credentials)), ('config', Path(config))]:
        if not path.exists() and not path.is_symlink():
            result['files'].append({'kind': kind, 'state': 'missing'})
            continue
        if path.is_symlink():
            result['files'].append({'kind': kind, 'state': 'symlink_not_followed'})
            continue
        version = fingerprint(path)
        parser = configparser.RawConfigParser()
        try:
            parser.read_string(path.read_text())
        except configparser.Error as exc:
            # ConfigParser messages can include the offending secret-bearing line.
            raise ValueError('Credential configuration could not be parsed') from None
        if fingerprint(path) != version:
            raise ValueError('Credential configuration changed during inventory')
        result['files'].append({'kind': kind, 'state': 'present', **version})
        for section in parser.sections():
            name = section.removeprefix('profile ')
            entry = profiles.setdefault(name, {'profile': name, 'static_key_present': False, 'mechanisms': [], 'iam_key_created_at': None, 'iam_key_age_days': None})
            access = parser.get(section, 'aws_access_key_id', fallback='')
            if access:
                entry['static_key_present'] |= not parser.has_option(section, 'aws_session_token')
            for option in ('credential_process', 'role_arn', 'source_profile', 'credential_source',
                           'sso_session', 'sso_start_url', 'aws_session_token'):
                if parser.has_option(section, option) and option not in entry['mechanisms']:
                    entry['mechanisms'].append(option)
    # Old credential backups need a preservation/revocation decision as well;
    # content hashes bind their versions without exposing keys or parsing prose.
    residues = set()
    for original in (Path(credentials), Path(config)):
        residues.update(original.parent.glob(original.name + '.backup*'))
        backup = original.with_name(original.name + '~')
        if backup.exists() or backup.is_symlink():
            residues.add(backup)
    result['residue_files'] = [
        {'path': str(path), 'state': 'symlink_not_followed'} if path.is_symlink()
        else {'path': str(path), **fingerprint(path), 'action': 'preserve_until_matching_key_revocation_is_verified'}
        for path in sorted(residues) if path.is_file() or path.is_symlink()
    ]
    result['profiles'] = sorted(profiles.values(), key=lambda item: item['profile'])
    return result


def graph_inventory():
    from psycopg2 import sql
    from src.db import get_connection
    tables, dependencies = [], []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute('SELECT current_database(), current_user, pg_postmaster_start_time()')
        database, user, started = cur.fetchone()
        for name in GRAPH_TABLES:
            cur.execute('SELECT to_regclass(%s)', ('public.' + name,))
            if cur.fetchone()[0] is None:
                tables.append({'table': name, 'state': 'absent'})
                continue
            cur.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier('public', name)))
            count = cur.fetchone()[0]
            cur.execute('''SELECT c.conname,c.conrelid::regclass::text,pg_get_constraintdef(c.oid)
                FROM pg_constraint c WHERE c.confrelid=to_regclass(%s) AND c.contype='f'
                ORDER BY c.conname''', ('public.' + name,))
            refs = [{'name': row[0], 'table': row[1], 'definition': row[2]} for row in cur.fetchall()]
            cur.execute('''SELECT DISTINCT n.nspname,c.relname,c.relkind
                FROM pg_depend d JOIN pg_rewrite r ON r.oid=d.objid
                JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE d.refobjid=to_regclass(%s) ORDER BY 1,2''', ('public.' + name,))
            views = [{'schema': row[0], 'view': row[1], 'kind': row[2]} for row in cur.fetchall()]
            external = [ref for ref in refs if ref['table'].removeprefix('public.') not in GRAPH_TABLES]
            dependencies.extend(external + views)
            tables.append({'table': name, 'state': 'present', 'rows': count, 'incoming_foreign_keys': refs, 'dependent_views': views})
    return {'observed_database': database, 'observed_user': user, 'server_started_at': str(started),
            'tables': tables, 'external_dependencies': dependencies,
            'proposed_statements': ['DROP TABLE public.' + name + ';' for name in GRAPH_TABLES if any(t['table'] == name and t['state'] == 'present' for t in tables)],
            'apply_ready': False, 'required': ['Verify whole-database recovery includes these exact table versions',
                                             'Confirm no installed writer remains enabled',
                                             'Resolve external dependencies and issue a new reviewed migration without CASCADE']}


def prepare(repo, installed, credentials, config, agent_dir, *, include_database=False):
    repo = Path(repo).resolve()
    disk = [{'root': relative, 'inventory': file_inventory(repo / relative),
             'action': 'preserve_encrypted_then_review_retirement', 'deletion_authorized': False}
            for relative in RETIREMENT_PATHS]
    # Mixed production/test telemetry is never classified by a historical count.
    disk[-1]['action'] = 'preserve_whole_file_then_identify_test_rows_before_any_filter'
    agents = [{'path': str(path), 'state': 'symlink_not_followed'} if path.is_symlink()
              else {'path': str(path), **fingerprint(path), 'action': 'review_orphaned_configuration'}
              for path in sorted(Path(agent_dir).glob('dream_cycle_*.json')) if path.is_file() or path.is_symlink()]
    return {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'repo': str(repo),
            'dry_run': True, 'applied': False, 'disk': disk, 'schedules': plist_inventory(repo, installed),
            'credentials': credential_inventory(credentials, config), 'orphaned_kiro_agents': agents,
            'graph': graph_inventory() if include_database else {'state': 'not_inspected', 'apply_ready': False},
            'prerequisites': ['Bind a verified encrypted preservation/restore receipt to this manifest',
                              'Recheck hashes immediately before any separately authorized mutation',
                              'Preserve current job enablement; this report does not approve deployment']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--launchagents', type=Path, default=Path.home() / 'Library/LaunchAgents')
    parser.add_argument('--credentials', type=Path, default=Path.home() / '.aws/credentials')
    parser.add_argument('--aws-config', type=Path, default=Path.home() / '.aws/config')
    parser.add_argument('--agent-dir', type=Path, default=Path.home() / '.kiro/agents')
    parser.add_argument('--include-database', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        plan = prepare(args.repo, args.launchagents, args.credentials, args.aws_config, args.agent_dir,
                       include_database=args.include_database)
        content = json.dumps(plan, sort_keys=True, indent=2) + '\n'
        with args.output.open('x') as handle:
            handle.write(content)
        print(json.dumps({'dry_run': True, 'manifest': str(args.output),
                          'sha256': hashlib.sha256(content.encode()).hexdigest(), 'model_calls': 0}))
        return 0
    except (OSError, ValueError, plistlib.InvalidFileException):
        print(json.dumps({'status': 'failed', 'reason': 'Inventory incomplete; no source was modified'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
