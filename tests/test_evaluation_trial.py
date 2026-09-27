"""Native pipeline qualification using real isolated SQL and scripted invokers.

Provider transport and native acquisition are intentionally absent. Human utility
is never inferred from synthetic votes or from this component qualification.
"""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src import db
from src.evaluation import trial
from tests.conftest import _deterministic_embedding


RECORDS=[
    {'source_type':'article','source_url':'test://workshop','title':'Workshop checklist',
     'content':'  # Workshop checklist\n---\nThe workshop uses a repeatable pilot checklist.  ', 'project':'workshop'},
    {'source_type':'article','source_url':'test://adoption','title':'Adoption plan',
     'content':'The adoption team needs a pilot readiness checklist.', 'project':'adoption'},
]


@pytest.fixture
def scripted_providers(monkeypatch,test_schema):
    from src.backends.bedrock import BedrockInvoker
    from src.backends.claude_code import ClaudeCodeInvoker
    calls=[]
    def invoke(self,system_prompt,user_message,**kwargs):
        stage=kwargs.get('stage');calls.append({'stage':stage,'system':system_prompt,'user':user_message})
        if stage=='skim':
            output={'summary':'A project discusses a pilot checklist.','people':[],'entities':[],
                    'topics':['pilot'],'decisions':[],'open_questions':[],
                    'novelty':1,'salience':2,'contradiction_hint':None,'doc_kind':'note'}
        elif stage=='explorer':
            rows=[row for row in db.list_memories() if row['parent_id'] is None]
            output=[{'name':'Reusable pilot checklist','strategy':'cross_project_collision',
                     'memory_ids':[str(row['id']) for row in rows],'memory_titles':[row['title'] for row in rows],
                     'hypothesis':'The adoption team could adapt the workshop checklist.'}]
        elif stage=='thinker':
            packet=json.loads(user_message)['memory_slice']
            output=[{'title':'Reuse the workshop pilot checklist','type':'insight','operation':'CREATE',
                     'content':'The adoption team could adapt the workshop pilot checklist for readiness review.',
                     'source_memories':packet['memory_ids'],'confidence':'medium','confidence_reasoning':'The sources support a possible reuse.',
                     'schema_operation':'assimilation','schema_note':'Shared readiness work','relationships':[],
                     'strategy_that_found_it':'cross_project_collision'}]
        elif stage=='grounding_audit' or stage.startswith('evaluator:'):
            output={'verdict':'REJECT' if stage=='evaluator:methodologist' else 'ACCEPT','score':85,
                    'reasoning':'Synthetic contract vote, not a real usefulness judgment.','blocking_issues':[],'advisory_issues':[]}
        else:
            raise AssertionError(f'Unscripted provider stage: {stage}')
        return {'output':output,'raw':json.dumps(output),'usage_source':'real',
                'usage':{'inputTokens':100,'outputTokens':50}}
    monkeypatch.setattr(BedrockInvoker,'invoke',invoke)
    monkeypatch.setattr(ClaudeCodeInvoker,'invoke',invoke)
    monkeypatch.setattr('src.embeddings.generate_embedding',_deterministic_embedding)
    monkeypatch.setattr('src.ingest.generate_embedding',_deterministic_embedding)
    monkeypatch.setattr('src.dream_cycle.storage.generate_embedding',_deterministic_embedding)
    # Explicitly opt this integration test into the already verified test database.
    monkeypatch.setattr('src.skim.store.get_connection',db.get_connection)
    return calls


def test_native_trial_persists_every_stage_and_leaves_human_outcomes_unknown(test_schema,clean_tables,tmp_path,scripted_providers):
    manifest=trial.prepare('How can the adoption pilot reuse the workshop checklist?',RECORDS)
    output=tmp_path/'trial'
    report=trial.run_trial(manifest,output,approved_sha256=trial.sha(manifest),fixture=True)
    assert report['fixture_models'] is True
    assert report['aborted'] is False
    assert report['candidate_count']==report['accepted_count']==1
    assert report['human_review'] is report['outcome'] is None
    assert report['embedding_cost_usd'] is None
    assert report['native_acquisition_and_distillation_exercised'] is False
    assert report['known_reasoning_usd']>0 and report['spend_incomplete'] is False
    stages=[call['stage'] for call in scripted_providers]
    assert stages.count('skim')==2
    assert stages.count('explorer')==stages.count('thinker')==stages.count('grounding_audit')==1
    assert {stage for stage in stages if stage.startswith('evaluator:')}=={
        'evaluator:skeptic','evaluator:advocate','evaluator:epistemologist','evaluator:methodologist'}
    assert all(stage['status']=='completed' for stage in json.loads((output/'stages.json').read_text()))
    assert set(json.loads((output/'private-arm-key.json').read_text()).values())=={'retrieval','discovery'}
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM memory_skim');assert cur.fetchone()[0]==2
        cur.execute("SELECT count(*) FROM memory_relationships WHERE relation_type='derived_from'");assert cur.fetchone()[0]==2
        cur.execute("SELECT content FROM memories WHERE source_url='test://workshop' AND parent_id IS NULL")
        assert cur.fetchone()[0]==RECORDS[0]['content']
        cur.execute('SELECT completed_at,cost_usd,stats FROM dream_cycle_runs')
        completed,cost,stats=cur.fetchone();assert completed and cost>0 and stats['skim_skimmed']==2
    review=trial.record_review(output,label='A',ratings={key:None for key in trial.DIMENSIONS},evidence='Review is still pending.')
    assert all(value is None for value in review['ratings'].values())


def test_trial_rejects_old_feedback_even_when_memories_empty(test_schema,clean_tables,tmp_path):
    from src.dream_cycle_db import create_run
    create_run('scheduled')
    manifest=trial.prepare('Review the pilot.',RECORDS)
    with pytest.raises(ValueError,match='empty|unrelated|application'):
        trial.run_trial(manifest,tmp_path/'no-output',approved_sha256=trial.sha(manifest),fixture=True)
    assert not (tmp_path/'no-output').exists()


def test_manifest_drift_fails_before_any_ingestion(test_schema,clean_tables,tmp_path):
    manifest=trial.prepare('Review the pilot.',RECORDS)
    approved=trial.sha(manifest);manifest['records'][0]['content']='Changed source'
    with pytest.raises(ValueError,match='hash changed'):
        trial.run_trial(manifest,tmp_path/'no-output',approved_sha256=approved,fixture=True)
    assert db.list_memories()==[]


def test_embedding_route_must_match_reviewed_manifest(test_schema,clean_tables,tmp_path,monkeypatch):
    manifest=trial.prepare('Review the pilot.',RECORDS)
    monkeypatch.setattr('src.embeddings.EMBEDDING_MODEL','unreviewed-provider-model')
    with pytest.raises(ValueError,match='[Ee]mbedding'):
        trial.run_trial(manifest,tmp_path/'no-output',approved_sha256=trial.sha(manifest),fixture=True)
    assert db.list_memories()==[]

def test_actual_connection_must_match_manifest_socket(test_schema,clean_tables,monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    original=db.get_connection
    @contextmanager
    def different_host_info():
        with original() as conn:
            yield SimpleNamespace(info=SimpleNamespace(host='/private/tmp/unrelated-socket',port=conn.info.port),cursor=conn.cursor)
    monkeypatch.setattr(db,'get_connection',different_host_info)
    with pytest.raises(ValueError,match='Actual database connection'):
        trial.assert_isolated()


def test_disabling_skim_rejects_protocol_before_writes(test_schema,clean_tables,tmp_path,monkeypatch):
    manifest=trial.prepare('Review the pilot.',RECORDS)
    monkeypatch.setenv('SECOND_BRAIN_SKIM_DISABLED','true')
    with pytest.raises(ValueError,match='requires skim'):
        trial.run_trial(manifest,tmp_path/'no-output',approved_sha256=trial.sha(manifest),fixture=True)
    assert db.list_memories()==[]
