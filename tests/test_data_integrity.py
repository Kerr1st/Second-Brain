"""Behavioral data-integrity tests against the disposable PostgreSQL cluster."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from hypothesis import given, strategies as st, settings
from psycopg2.pool import PoolError, ThreadedConnectionPool

from src import db
from src.db.maintenance import plan_dedup,apply_dedup,plan_rechunk,apply_rechunk,digest
from src.embeddings import EmbeddingInputTooLarge
from src.ingest import chunk_by_section,ingest_content,ingest_batch


def vector(text=''):
    return [1.0]+[0.0]*1023


@given(st.text(),st.integers(min_value=1,max_value=400))
@settings(max_examples=80)
def test_chunks_are_bounded_lossless(body,bound):
    chunks=chunk_by_section(body,bound)
    assert ''.join(chunks)==body
    assert all(0<len(chunk)<=bound for chunk in chunks)


def test_embedding_oversize_rejected_before_client(monkeypatch):
    import src.embeddings as e
    client=Mock();monkeypatch.setattr(e,'_get_client',client)
    with pytest.raises(EmbeddingInputTooLarge) as failure:e.generate_embedding('x'*11,max_chars=10)
    assert failure.value.chars==11
    client.assert_not_called()


def test_parent_chunks_all_or_nothing(test_db,clean_tables,monkeypatch):
    import src.ingest as ingest
    calls=0
    def fail_second(text):
        nonlocal calls
        calls+=1
        if calls==2:raise ValueError('synthetic provider failure')
        return vector()
    monkeypatch.setattr(ingest,'generate_embedding',fail_second)
    with pytest.raises(ValueError):ingest_content('x'*8001,'article','test://atomic')
    assert not db.list_memories()
    monkeypatch.setattr(ingest,'generate_embedding',lambda _:vector())
    original=ingest.create_memory
    def fail_chunk(**kwargs):
        if kwargs.get('parent_id'):raise ValueError('synthetic store failure')
        return original(**kwargs)
    monkeypatch.setattr(ingest,'create_memory',fail_chunk)
    with pytest.raises(ValueError):ingest_content('x'*80,'article','test://atomic')
    assert not db.list_memories()


def test_batch_identity_and_refresh_preserve_citations(test_db,clean_tables,monkeypatch):
    import src.ingest as ingest
    monkeypatch.setattr(ingest,'generate_embedding',lambda _:vector())
    monkeypatch.setattr(ingest,'_discover_relationships',lambda *a:None)
    doc='# Example\nProject: alpha\n---\nOriginal content'
    assert ingest_batch([('test://one',doc),('test://one',doc),(None,doc)],'article')=={'processed':1,'skipped':1,'failed':1}
    parents=db.list_memories();parent=next(r for r in parents if not r['parent_id']);old=next(r for r in parents if r['parent_id'])
    # Project is descriptive, not identity (F-028): the same channel+URL under another project is the same source.
    assert ingest_content(doc,'article','test://one',project='beta') is None
    assert len([r for r in db.list_memories() if not r['parent_id']])==1
    assert ingest_content(doc+' updated','article','test://one',refresh=True)==str(parent['id'])
    assert db.get_memory(old['id'])['status']=='superseded'
    assert db.get_memory(parent['id'])['content']=='Original content updated'
    assert ingest_content(doc+' updated','article','test://one',refresh=True) is None


def test_transaction_metadata_and_concurrent_updates(test_db,clean_tables):
    mid=db.create_memory('idea','A','content',metadata={'stable':True})
    def patch(i):db.update_fields(mid,{},metadata_patch={f'key{i}':i})
    with ThreadPoolExecutor(max_workers=4) as executor:list(executor.map(patch,range(12)))
    row=db.get_memory(mid)
    assert row['metadata']=={'stable':True,**{f'key{i}':i for i in range(12)}}
    with pytest.raises(ValueError):
        with db.transaction() as conn:
            db.update_memory(mid,title='rolled back',conn=conn)
            raise ValueError('fail')
    assert db.get_memory(mid)['title']=='A'
    assert isinstance(db._pool,ThreadedConnectionPool)


def test_relationship_age_provenance_neighbor_and_symmetry(test_db,clean_tables):
    ids=[db.create_memory('idea',str(i),'evidence') for i in range(4)]
    db.create_relationship(ids[0],ids[1],'related_to')
    db.create_relationship(ids[1],ids[0],'related_to')
    db.create_relationship(ids[0],ids[2],'derived_from')
    db.create_relationship(ids[0],ids[3],'supports')
    assert len(db.get_relationships(ids[0]))==3
    assert {str(r['neighbor_id']) for r in db.get_relationships(ids[0])}==set(ids[1:])
    assert db.expire_stale_relationships()==0
    with db.transaction() as conn,conn.cursor() as cur:
        cur.execute("UPDATE memory_relationships SET created_at=now()-interval '100 days'")
    assert db.expire_stale_relationships()==2
    assert [r['relation_type'] for r in db.get_relationships(ids[0])]==['derived_from']
    assert len(db.get_relationships(ids[0],include_expired=True))==3


def test_dedup_preserves_identity_and_untyped_references(test_db,clean_tables):
    a=db.create_memory('source','A','same',source_type='article',source_url='test://a',project='alpha',metadata={'origin':'first'})
    b=db.create_memory('source','A','same',source_type='article',source_url='test://a',project='alpha',metadata={'origin':'second'})
    distinct=db.create_memory('source','B','same',source_type='article',source_url='test://b',project='alpha')
    other=db.create_memory('source','C','same',source_type='article',source_url='test://a',project='beta')
    derived=[db.create_memory('insight','D','same',source_type='distilled_agent_task',source_url='test://a') for _ in range(2)]
    citation=db.create_memory('insight','Citation','literal '+b,metadata={'source_memories':[b]})
    db.create_relationship(citation,b,'derived_from')
    with db.transaction() as conn,conn.cursor() as cur:
        cur.execute("INSERT INTO dream_cycle_runs(run_type) VALUES('user_triggered') RETURNING id");run=cur.fetchone()[0]
        cur.execute("INSERT INTO dream_cycle_candidates(run_id,target_memory_id,created_memory_id,candidate_json) VALUES(%s,%s,%s,%s)",(str(run),b,b,json.dumps({'source_memories':[b]})))
        cur.execute("INSERT INTO memory_skim(memory_id,model,prompt_version,skim) VALUES(%s,'fake','v1','{}'),(%s,'fake','v1','{\"evidence\":\"retained\"}')",(a,b))
    plan=plan_dedup();assert len(plan['operations'])==1
    assert apply_dedup(plan,digest(plan))==1
    assert apply_dedup(plan,digest(plan))==0
    assert db.get_memory(b) is None
    assert db.get_memory(citation)['metadata']['source_memories']==[a]
    assert db.get_memory(citation)['content']=='literal '+b
    assert db.get_memory(a)['metadata']['dedup_archived_skims'][0]['skim']=={'evidence':'retained'}
    assert str(db.get_relationships(citation)[0]['neighbor_id'])==a
    assert all(db.get_memory(mid) for mid in [distinct,other,*derived])
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute('SELECT target_memory_id::text,created_memory_id::text,candidate_json FROM dream_cycle_candidates')
        target,created,body=cur.fetchone();assert target==created==a;assert body['source_memories']==[a]


def test_rechunk_is_resumable_and_purges_unreferenced_old_chunks(test_db,clean_tables,tmp_path):
    parent=db.create_memory('source','Long','x'*9000,source_type='article',source_url='test://long')
    old=db.create_memory('source','Old chunk','x'*9000,parent_id=parent,source_type='article',source_url='test://long',embedding=vector())
    plan=plan_rechunk();assert plan['embedding_calls']==3
    embed=Mock(side_effect=vector)
    assert apply_rechunk(plan,digest(plan),embed=embed,cache_dir=tmp_path)==1
    # Fix 7 / review B N4: the superseded chunk nothing cites is gone, not kept forever.
    assert db.get_memory(old) is None and db.get_memory(parent)['status']=='active'
    assert apply_rechunk(plan,digest(plan),embed=embed,cache_dir=tmp_path)==0
    assert embed.call_count==2 # identical 4k chunks reuse prepared vectors
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute("SELECT content FROM memories WHERE parent_id=%s AND status='active' ORDER BY (metadata->>'chunk_index')::int",(parent,))
        assert ''.join(r[0] for r in cur.fetchall())=='x'*9000

def test_source_identity_concurrent_insert(test_db,clean_tables,monkeypatch):
    import threading
    import src.ingest as ingest
    barrier=threading.Barrier(2)
    def embed(_):barrier.wait(timeout=5);return vector()
    monkeypatch.setattr(ingest,'generate_embedding',embed)
    monkeypatch.setattr(ingest,'_discover_relationships',lambda *a:None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:ingest_content('Concurrent source','article','test://same'),range(2)))
    assert sum(r is not None for r in results)==1
    assert len(db.list_memories())==2


def test_metadata_only_refresh_does_not_embed(test_db,clean_tables,monkeypatch):
    import src.ingest as ingest
    embed=Mock(side_effect=vector)
    monkeypatch.setattr(ingest,'generate_embedding',embed)
    monkeypatch.setattr(ingest,'_discover_relationships',lambda *a:None)
    fields={'type':'decision','title':'Decision','confidence':0.8,'tags':['old']}
    parent=ingest_content('Unchanged text','quick_desktop_decision','qd-decision://test',memory_fields=fields,source_metadata={'origin':'initial'})
    assert ingest_content('Unchanged text','quick_desktop_decision','qd-decision://test',refresh=True,memory_fields={**fields,'confidence':0.9,'tags':['new']},source_metadata={'origin':'updated'})==parent
    assert embed.call_count==1
    row=db.get_memory(parent)
    # R4 (D N-4): captured tags are merged into the row's tags, never substituted for them.
    assert row['confidence']==0.9 and row['tags']==['old','new'] and row['metadata']['origin']=='updated'


def test_pool_exhaustion_times_out(test_db,monkeypatch):
    db.close_pool();monkeypatch.setenv('DB_POOL_MAX','1');monkeypatch.setenv('DB_POOL_MIN','1');monkeypatch.setenv('DB_POOL_TIMEOUT','0.02')
    try:
        with db.get_connection():
            started=time.monotonic()
            with pytest.raises(PoolError):
                with db.get_connection():pass
            assert time.monotonic()-started<1
    finally:db.close_pool()


def test_dedup_refuses_unknown_foreign_keys_and_rolls_back(test_db,clean_tables):
    from tests.conftest import owner_connect
    first=db.create_memory('source','A','body',source_type='article',source_url='test://unknown')
    second=db.create_memory('source','A','body',source_type='article',source_url='test://unknown')
    plan=plan_dedup()
    owner=owner_connect();owner.autocommit=True
    try:
        # DDL is the owner's; the app role (017) cannot create tables.
        with owner.cursor() as cur:
            cur.execute('CREATE TABLE maintenance_unknown_ref(memory_id uuid REFERENCES memories(id))')
        with db.transaction() as conn,conn.cursor() as cur:
            cur.execute('INSERT INTO maintenance_unknown_ref VALUES(%s)',(second,))
        with pytest.raises(ValueError,match='Unrecognized references'):apply_dedup(plan,digest(plan))
        assert db.get_memory(first) and db.get_memory(second)
    finally:
        with owner.cursor() as cur:cur.execute('DROP TABLE IF EXISTS maintenance_unknown_ref')
        owner.close()


def test_rechunk_storage_failure_reuses_prepared_vectors(test_db,clean_tables,tmp_path,monkeypatch):
    import src.db.maintenance as maintenance
    parent=db.create_memory('source','Recovery','x'*5000,source_type='article',source_url='test://recovery')
    plan=plan_rechunk();embed=Mock(side_effect=vector)
    original=maintenance.create_memory
    monkeypatch.setattr(maintenance,'create_memory',Mock(side_effect=ValueError('synthetic write failure')))
    with pytest.raises(ValueError,match='write failure'):apply_rechunk(plan,digest(plan),embed=embed,cache_dir=tmp_path)
    assert len(db.list_memories())==1
    monkeypatch.setattr(maintenance,'create_memory',original)
    assert apply_rechunk(plan,digest(plan),embed=embed,cache_dir=tmp_path)==1
    assert embed.call_count==2

def test_dedup_rejects_status_drift(test_db,clean_tables):
    first=db.create_memory('source','A','same',source_type='article',source_url='test://drift')
    duplicate=db.create_memory('source','A','same',source_type='article',source_url='test://drift')
    plan=plan_dedup();db.update_memory(duplicate,status='archived')
    with pytest.raises(ValueError,match='status changed'):apply_dedup(plan,digest(plan))
    assert db.get_memory(first) and db.get_memory(duplicate)


def test_release_repairs_are_dry_run_and_resumable(test_db,clean_tables):
    from uuid import uuid4
    from src.db.maintenance import plan_metadata_repairs,apply_metadata_repairs,plan_role_audit
    project=str(uuid4());mid=db.create_memory('source','Legacy','content',project=project,source_url='test://legacy')
    plan=plan_metadata_repairs()
    assert db.get_memory(mid)['project']==project
    assert apply_metadata_repairs(plan,digest(plan))==1
    assert apply_metadata_repairs(plan,digest(plan))==0
    row=db.get_memory(mid);assert row['project'] is None;assert row['metadata']['legacy_uuid_project']==project
    # The suite runs as the separated app role (017 already applied by the owner):
    # the audit finds nothing left to do and never proposes DDL of its own.
    roles=plan_role_audit()
    assert roles['kind']=='role-audit' and roles['currently_superuser'] is False
    assert roles['application_role']!=roles['owner_role'] and roles['app_is_owner_member'] is False
    assert roles['objects_not_owned_by_owner']==[] and roles['structural_privileges']==[]
    assert roles['remaining_steps']==[] and 'proposed_sql' not in roles


def test_dedup_remaps_slice_array_and_feedback(test_db,clean_tables):
    first=db.create_memory('source','A','same',source_type='article',source_url='test://refs')
    duplicate=db.create_memory('source','A','same',source_type='article',source_url='test://refs')
    other=db.create_memory('idea','Related','work')
    with db.transaction() as conn,conn.cursor() as cur:
        cur.execute("INSERT INTO dream_cycle_runs(run_type) VALUES('scheduled') RETURNING id");run=str(cur.fetchone()[0])
        cur.execute("INSERT INTO dream_cycle_slices(run_id,position,memory_ids,memory_titles) VALUES(%s,0,%s,%s)",(run,[duplicate,other],['A','Related']))
        cur.execute("INSERT INTO express_feedback(target_type,target_key,signal,weight) VALUES('item',%s,'useful',1)",(duplicate+':'+other,))
    plan=plan_dedup();apply_dedup(plan,digest(plan))
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute('SELECT memory_ids FROM dream_cycle_slices');assert cur.fetchone()[0]==[first,other]
        cur.execute('SELECT target_key FROM express_feedback');assert cur.fetchone()[0]==first+':'+other

def test_opt_in_context_repair_preserves_parent_verbatim(test_db,clean_tables,tmp_path):
    text='My actual question\n<EnvironmentContext>Repeated IDE context</EnvironmentContext>\nAnswer and followup.'
    parent=db.create_memory('source','IDE',''+text,source_type='kiro_ide_chat',source_url='chat://context')
    child=db.create_memory('source','Old',text,embedding=vector(),parent_id=parent,source_type='kiro_ide_chat',source_url='chat://context',metadata={'chunk_index':0})
    assert plan_rechunk()['operations']==[]
    plan=plan_rechunk(strip_known_ide_context=True)
    apply_rechunk(plan,digest(plan),embed=vector,cache_dir=tmp_path)
    assert db.get_memory(parent)['content']==text
    assert db.get_memory(child) is None  # unreferenced old chunk purged
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute("SELECT content FROM memories WHERE parent_id=%s AND status='active'",(parent,))
        assert cur.fetchone()[0]=='My actual question\n\nAnswer and followup.'


@pytest.mark.parametrize('layout', ['prefix', 'wrong_order', 'duplicate_index', 'stale_content'])
def test_rechunk_detects_incomplete_or_changed_chunk_sequence(test_db,clean_tables,tmp_path,layout):
    content='a'*4000+'b'*1000
    parent=db.create_memory('source','Partial',content,source_type='article',source_url='test://partial')
    chunks=[('a'*4000,0),('b'*1000,1)]
    if layout=='prefix': chunks=chunks[:1]
    elif layout=='wrong_order': chunks=[(text,1-index) for text,index in chunks]
    elif layout=='duplicate_index': chunks=[(text,0) for text,index in chunks]
    elif layout=='stale_content': chunks[1]=('old tail',1)
    original=[]
    for text,index in chunks:
        original.append(db.create_memory('source','Existing',text,embedding=vector(),parent_id=parent,source_type='article',source_url='test://partial',metadata={'chunk_index':index}))
    plan=plan_rechunk()
    assert [op['memory_id'] for op in plan['operations']]==[parent]
    assert plan['embedding_calls']==2
    assert apply_rechunk(plan,digest(plan),embed=vector,cache_dir=tmp_path)==1
    assert all(db.get_memory(mid) is None for mid in original)  # unreferenced old chunks purged
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute("SELECT content FROM memories WHERE parent_id=%s AND status='active' ORDER BY (metadata->>'chunk_index')::int",(parent,))
        assert ''.join(row[0] for row in cur.fetchall())==content
    assert not plan_rechunk()['operations']


@pytest.mark.parametrize('bad_vector', [[0.0], [float('nan')]+[0.0]*1023, [float('inf')]+[0.0]*1023])
def test_rechunk_invalid_vectors_do_not_poison_resume(test_db,clean_tables,tmp_path,bad_vector):
    parent=db.create_memory('source','Retry','content',source_type='article',source_url='test://badvector')
    plan=plan_rechunk()
    with pytest.raises(ValueError,match='Invalid prepared embedding'):
        apply_rechunk(plan,digest(plan),embed=lambda _:bad_vector,cache_dir=tmp_path)
    assert not list(tmp_path.iterdir())
    assert len(db.list_memories())==1
    assert apply_rechunk(plan,digest(plan),embed=vector,cache_dir=tmp_path)==1
    assert db.get_memory(parent)['status']=='active'


@pytest.mark.parametrize('during_preparation', [False,True])
def test_rechunk_rejects_parent_status_drift(test_db,clean_tables,tmp_path,during_preparation):
    parent=db.create_memory('source','Status','content',source_type='article',source_url='test://status')
    plan=plan_rechunk()
    embed=Mock(side_effect=vector)
    if during_preparation:
        def archive(_):
            db.update_memory(parent,status='archived')
            return vector()
        embed.side_effect=archive
    else:
        db.update_memory(parent,status='archived')
    with pytest.raises(ValueError,match='Memory changed'):
        apply_rechunk(plan,digest(plan),embed=embed,cache_dir=tmp_path)
    assert embed.call_count==int(during_preparation)
    assert db.get_memory(parent)['status']=='archived'
    assert len(db.list_memories(status='archived'))==1
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM memories WHERE parent_id=%s',(parent,));assert cur.fetchone()[0]==0
        cur.execute('SELECT count(*) FROM corpus_maintenance WHERE manifest_sha256=%s',(digest(plan),));assert cur.fetchone()[0]==0


def test_relationships_preserve_directed_reverse_edges_and_notes(test_db,clean_tables):
    a=db.create_memory('idea','A','Alpha');b=db.create_memory('idea','B','Beta')
    db.create_relationship(a,b,'supports','A supports B')
    db.create_relationship(b,a,'supports','B supports A')
    with db.transaction() as conn,conn.cursor() as cur:
        cur.execute("INSERT INTO memory_relationships(source_id,target_id,relation_type,note) VALUES(%s,%s,'related_to','legacy forward'),(%s,%s,'related_to','legacy reverse')",(a,b,b,a))
    edges=db.get_relationships(a)
    directed=[edge for edge in edges if edge['relation_type']=='supports']
    assert {(str(edge['source_id']),str(edge['target_id']),edge['note']) for edge in directed}=={(a,b,'A supports B'),(b,a,'B supports A')}
    assert len([edge for edge in edges if edge['relation_type']=='related_to'])==1
    assert all(str(edge['neighbor_id'])==b for edge in edges)


def test_rechunk_keeps_cited_old_chunks_as_superseded_history(test_db,clean_tables,tmp_path):
    """Fix 7 / review B N4: a superseded chunk that a relationship, a candidate ledger
    row or a slice array still cites is retained (status superseded); the rest are deleted."""
    from src.dream_cycle_db import create_run, store_candidate
    parent=db.create_memory('source','Doc','a'*4000+'b'*1000,source_type='article',source_url='test://cited')
    cited=db.create_memory('source','Cited chunk','a'*4000,embedding=vector(),parent_id=parent,source_type='article',source_url='test://cited',metadata={'chunk_index':0})
    ledgered=db.create_memory('source','Ledgered chunk','stale',embedding=vector(),parent_id=parent,source_type='article',source_url='test://cited',metadata={'chunk_index':1})
    loose=db.create_memory('source','Loose chunk','also stale',embedding=vector(),parent_id=parent,source_type='article',source_url='test://cited',metadata={'chunk_index':2})
    other=db.create_memory('idea','Other','x',embedding=vector())
    db.create_relationship(other,cited,'supports')
    run=create_run('user_triggered')
    store_candidate(run,{'title':'c','source_memories':[ledgered]},{},'REJECTED')
    plan=plan_rechunk()
    assert [op['memory_id'] for op in plan['operations']]==[parent]
    assert apply_rechunk(plan,digest(plan),embed=vector,cache_dir=tmp_path)==1
    assert db.get_memory(cited)['status']=='superseded' and db.get_memory(ledgered)['status']=='superseded'
    assert db.get_memory(loose) is None
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute("SELECT string_agg(content,'' ORDER BY (metadata->>'chunk_index')::int) FROM memories WHERE parent_id=%s AND status='active'",(parent,))
        assert cur.fetchone()[0]=='a'*4000+'b'*1000
        cur.execute("SELECT details FROM corpus_maintenance WHERE operation_key=%s",(parent,))
        details=cur.fetchone()[0]
    assert details['deleted']==1 and details['retained_referenced']==2


def test_plan_rechunk_leaves_complete_embedded_leaves_alone(test_db,clean_tables):
    """Fix 7 / review B N3: 35,100 live small, embedded, childless ``type='source'`` rows
    (quick_desktop_*) must not each receive a redundant child copy of themselves."""
    complete=db.create_memory('source','QD fact','Short captured fact.',embedding=vector(),source_type='quick_desktop_decision',source_url='qd://entity/1',metadata={'capture_identity':True})
    unembedded=db.create_memory('source','Half written','Unembedded leaf text.',source_type='article',source_url='test://half')
    oversize=db.create_memory('source','Big leaf','z'*9000,embedding=vector(),source_type='article',source_url='test://big')
    plan=plan_rechunk()
    ops={op['memory_id']:op for op in plan['operations']}
    assert complete not in ops and plan['skipped_complete_leaves']==1
    assert ops[unembedded]['mode']=='leaf' and len(ops[unembedded]['chunk_hashes'])==1
    assert ops[oversize]['mode']=='chunks' and len(ops[oversize]['chunk_hashes'])==3
    assert plan['embedding_calls']==4


def test_planners_stream_without_fetching_corpus_text(test_db,clean_tables,monkeypatch):
    """Fix 7 / review B N5: plan_dedup never selects ``content``; plan_rechunk fetches
    one parent's text at a time and only for rows that need a decision."""
    from src.db import maintenance as m
    for i in range(5):
        db.create_memory('source',f'Leaf {i}',f'leaf text {i}',embedding=vector(),source_type='quick_desktop_people',source_url=f'qd://memory/{i}')
    parent=db.create_memory('source','Doc','d'*5000,source_type='article',source_url='test://stream')
    db.create_memory('source','chunk','d'*4000,embedding=vector(),parent_id=parent,source_type='article',source_url='test://stream',metadata={'chunk_index':0})
    statements=[];named=[]
    from psycopg2.extras import RealDictCursor
    original_execute=RealDictCursor.execute;original_init=RealDictCursor.__init__
    def spy_init(self,*args,**kwargs):
        named.append(kwargs.get('name') or (args[1] if len(args)>1 else None));return original_init(self,*args,**kwargs)
    def spy_execute(self,query,vars=None):
        statements.append(query if isinstance(query,str) else query.as_string(self.connection));return original_execute(self,query,vars)
    monkeypatch.setattr(RealDictCursor,'__init__',spy_init);monkeypatch.setattr(RealDictCursor,'execute',spy_execute)
    assert plan_dedup()['operations']==[]
    assert any(n for n in named)  # a server-side (named) cursor streamed the corpus
    assert all('sha256' in q or 'content' not in q for q in statements)  # dedup never fetches text
    statements.clear()
    plan=plan_rechunk()
    content_fetches=[q for q in statements if q.strip().startswith('SELECT content FROM memories WHERE id')]
    assert len(content_fetches)==1 and [op['memory_id'] for op in plan['operations']]==[parent]


def test_transaction_timeout_profiles(test_db,clean_tables,monkeypatch):
    """Fix 11 / review B N8: ordinary writers get the bounded defaults; maintenance
    repairs run without a statement timeout and a longer lock wait; both configurable."""
    from src.db import pool
    def gucs(**kw):
        with pool.transaction(**kw) as conn,conn.cursor() as cur:
            cur.execute("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
            return cur.fetchone()
    assert gucs()==('30s','5s')
    assert gucs(timeouts='maintenance')==('0','1min')
    monkeypatch.setenv('DB_STATEMENT_TIMEOUT_MS','120000');monkeypatch.setenv('DB_LOCK_TIMEOUT_MS','250')
    monkeypatch.setenv('DB_MAINTENANCE_STATEMENT_TIMEOUT_MS','3600000');monkeypatch.setenv('DB_MAINTENANCE_LOCK_TIMEOUT_MS','0')
    assert gucs()==('2min','250ms') and gucs(timeouts='maintenance')==('1h','0')
    with pytest.raises(ValueError):gucs(timeouts='nope')
    monkeypatch.setenv('DB_STATEMENT_TIMEOUT_MS','-1')
    with pytest.raises(ValueError):gucs()
    # A pooled connection outside transaction() carries no leaked timeout.
    with db.get_connection() as conn,conn.cursor() as cur:
        cur.execute("SELECT current_setting('statement_timeout')");assert cur.fetchone()[0]=='0'
