"""Read-only release preparation retains source files and omits credential values."""
import json
import plistlib
from pathlib import Path

import pytest

from scripts.maintenance import prepare_operational_retirement as retirement


def fixture_tree(tmp_path):
    repo = tmp_path / 'repo'
    installed = tmp_path / 'installed'
    (repo / 'scheduling').mkdir(parents=True)
    installed.mkdir()
    for label in ['same', 'changed', 'linked', 'absent']:
        path = repo / 'scheduling' / f'com.second-brain.{label}.plist'
        path.write_bytes(plistlib.dumps({'Label': f'com.second-brain.{label}'}))
        if label == 'same':
            (installed / path.name).write_bytes(path.read_bytes())
        elif label == 'changed':
            (installed / path.name).write_bytes(plistlib.dumps({'Label': 'older.configuration'}))
        elif label == 'linked':
            (installed / path.name).symlink_to(path)
    private = repo / 'scripts/backups/kb-sources'
    private.mkdir(parents=True)
    (private / 'page.md').write_text('synthetic private document never printed')
    (private / 'outside').symlink_to(tmp_path)
    credentials = tmp_path / 'credentials'
    credentials.write_text('[second-brain-jobs]\naws_access_key_id=AKIASYNTHETIC\naws_secret_access_key=do-not-export-this\n')
    (tmp_path / 'credentials.backup').write_text('historical secret do-not-export-this')
    config = tmp_path / 'config'
    config.write_text('[profile proposed]\ncredential_process=/private/provider --secret=do-not-export-this\n')
    agents = tmp_path / 'agents'; agents.mkdir()
    (agents / 'dream_cycle_old.json').write_text('{"private":"not a model input"}')
    return repo, installed, credentials, config, agents


def test_manifest_has_versions_without_secret_or_source_contents(tmp_path):
    args = fixture_tree(tmp_path)
    before = retirement.fingerprint(args[2])
    plan = retirement.prepare(*args)
    encoded = json.dumps(plan)
    assert 'do-not-export-this' not in encoded and 'AKIASYNTHETIC' not in encoded
    assert 'synthetic private document' not in encoded and 'not a model input' not in encoded
    assert retirement.fingerprint(args[2]) == before
    assert len(plan['credentials']['residue_files']) == 1
    assert plan['credentials']['residue_files'][0]['sha256']
    assert plan['applied'] is False and plan['graph']['state'] == 'not_inspected'
    assert plan['credentials']['profiles'][1]['static_key_present'] is True
    states = {row['label'].split('.')[-1]: row['state'] for row in plan['schedules']}
    assert states == {'same': 'identical_copy', 'changed': 'divergent_copy', 'linked': 'linked_to_target', 'absent': 'not_installed'}
    assert plan['disk'][0]['inventory']['symlinks'] == ['kb-sources/outside']
    assert len(plan['disk'][0]['inventory']['files']) == 1
    assert (args[1] / 'com.second-brain.same.plist').is_symlink() is False


def test_cli_output_is_private_exclusive_and_no_apply_mode(tmp_path, capsys):
    repo, installed, credentials, config, agents = fixture_tree(tmp_path)
    output = tmp_path / 'manifest.json'
    args = ['--repo', str(repo), '--launchagents', str(installed), '--credentials', str(credentials),
            '--aws-config', str(config), '--agent-dir', str(agents), '--output', str(output)]
    assert retirement.main(args) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert retirement.main(args) == 1
    assert 'do-not-export-this' not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        retirement.main([*args, '--apply'])


def test_broken_credential_line_does_not_expose_secret(tmp_path):
    args = fixture_tree(tmp_path)
    args[2].write_text('aws_secret_access_key=do-not-export-this\n')
    with pytest.raises(ValueError) as err:
        retirement.prepare(*args)
    assert 'do-not-export-this' not in str(err.value)


def test_fingerprint_rejects_symlink_and_reports_drift(tmp_path, monkeypatch):
    path = tmp_path / 'data'; path.write_text('before')
    link = tmp_path / 'link'; link.symlink_to(path)
    with pytest.raises(ValueError):
        retirement.fingerprint(link)
    original = retirement.os.open
    def change_after_stat(name, flags):
        path.write_text('changed content')
        return original(name, flags)
    monkeypatch.setattr(retirement.os, 'open', change_after_stat)
    with pytest.raises(ValueError, match='changed'):
        retirement.fingerprint(path)


def test_graph_inventory_uses_real_schema_without_mutation(test_db, clean_tables):
    from src.db import get_connection
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        count = cur.fetchone()[0]
    plan = retirement.graph_inventory()
    assert plan['apply_ready'] is False
    assert next(row for row in plan['tables'] if row['table'] == 'entities')['rows'] == count
    assert plan['proposed_statements'] == ['DROP TABLE public.memory_entities;', 'DROP TABLE public.entity_edges;', 'DROP TABLE public.entities;']
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM entities')
        assert cur.fetchone()[0] == count
