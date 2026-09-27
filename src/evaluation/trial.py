"""Bounded native discovery over frozen normalized sources, with a retrieval baseline.

This does not claim native acquisition or distillation was exercised. It records
those omissions and leaves human novelty/usefulness/outcomes unscored. Execution
requires an isolated target and an exact source/configuration authorization hash.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import tomllib

ROOT=Path(__file__).resolve().parents[2]
DIMENSIONS=('grounding','transfer','novelty','timeliness','usefulness')


def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def prepare(task, records, *, cap_usd=5.0):
    from src.validation import require_text, SOURCE_TYPES, one_of
    if not 1<=len(records)<=35: raise ValueError('A trial requires 1–35 frozen records')
    if not isinstance(cap_usd,(int,float)) or isinstance(cap_usd,bool) or not 0<cap_usd<=9:
        raise ValueError('Trial reasoning threshold must be finite, positive and at most $9')
    clean=[];seen=set()
    for record in records:
        r={key:record[key] for key in ('source_type','source_url','title','content','project')}
        for key in ('source_url','title','content'):require_text(r[key],key)
        one_of(r['source_type'],SOURCE_TYPES,'source_type')
        if len(r['content'])>48000: raise ValueError('Split the approved source explicitly; do not silently truncate')
        identity=(r['source_type'],r['source_url'],r['project'])
        if identity in seen:raise ValueError('Repeated source identity in trial')
        seen.add(identity);r['sha256']=sha(r);clean.append(r)
    config=tomllib.loads((ROOT/'config/backends.toml').read_text())
    manifest={'version':1,'task':require_text(task,'task'),'records':clean,
              'profile':'controlled','roles':config['profiles']['controlled'],
              'configuration_sha256':hashlib.sha256((ROOT/'config/backends.toml').read_bytes()).hexdigest(),
              'destinations':['AWS Bedrock: embedding and direct reasoning', 'Claude Code Explorer: verify Bedrock routing before execution'],
              'reasoning_stop_threshold_usd':float(cap_usd),'threshold_can_overshoot':True,
              'embedding_model':'amazon.titan-embed-text-v2:0','embedding_cost_included_in_threshold':False,
              'omitted_stages':['native source acquisition','conversation distillation','external delivery','human uptake/outcome'],
              'skim_configuration':tomllib.loads((ROOT/'config/skim.toml').read_text()),
              'skim_configuration_sha256':hashlib.sha256((ROOT/'config/skim.toml').read_bytes()).hexdigest(),
              'authorization':None}
    return manifest


def assert_isolated():
    """A verified private cluster, never a mere database-name convention."""
    marker=os.environ.get('SECOND_BRAIN_EVALUATION_MANIFEST') or os.environ.get('SECOND_BRAIN_TEST_MANIFEST')
    if not marker:raise ValueError('An isolated evaluation cluster manifest is required')
    path=Path(marker).resolve();data=json.loads(path.read_text());base=path.parent
    if base.parent!=Path('/private/tmp') or not base.name.startswith(('sbtest-','sbeval-')):
        raise ValueError('Evaluation must use a newly created private temporary cluster')
    if Path(data['host']).resolve()!=base/'socket' or Path(data['data_directory']).resolve()!=base/'data':
        raise ValueError('Invalid isolated cluster identity')
    for env,key in [('DB_HOST','host'),('DB_USER','user')]:
        if os.environ.get(env)!=data[key]:raise ValueError('Evaluation target differs from manifest')
    if os.environ.get('DB_NAME') not in data['databases']:raise ValueError('Unknown evaluation database')
    from src.db import get_connection
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT current_database(), current_user, inet_server_addr()')
        db,user,host=cur.fetchone()
        actual_host=conn.info.host
        actual_port=conn.info.port
    directory=(base/'data'/'postmaster.pid').read_text().splitlines()[1]
    if Path(actual_host).resolve()!=base/'socket' or actual_port!=int(data.get('port',5432)):
        raise ValueError('Actual database connection differs from isolated manifest')
    if db!=os.environ['DB_NAME'] or user!=data['user'] or host is not None or Path(directory).resolve()!=base/'data':
        raise ValueError('Connected server is not the isolated evaluation cluster')


def run_trial(manifest, output, *, approved_sha256, fixture=False):
    if sha(manifest)!=approved_sha256:raise ValueError('Reviewed manifest hash changed')
    if manifest['configuration_sha256']!=hashlib.sha256((ROOT/'config/backends.toml').read_bytes()).hexdigest():
        raise ValueError('Role configuration changed after review')
    if fixture:
        if not os.environ.get('SECOND_BRAIN_TEST_MANIFEST'):raise ValueError('Fixture mode requires offline test runner')
    elif os.environ.get('CLAUDE_CODE_USE_BEDROCK')!='1':
        raise ValueError('Verify and explicitly configure Explorer Bedrock routing before private transfer')
    if manifest['skim_configuration_sha256']!=hashlib.sha256((ROOT/'config/skim.toml').read_bytes()).hexdigest():
        raise ValueError('Skim configuration changed after review')
    if os.environ.get('SECOND_BRAIN_SKIM_DISABLED','').lower() in {'1','true','yes'}:
        raise ValueError('Trial requires skim; disabling it changes the approved protocol')
    if os.environ.get('SECOND_BRAIN_KILL_SWITCH','').lower() in {'1','true','yes'}:
        raise ValueError('Kill switch refuses trial before embedding or model calls')
    configured=os.environ.get('SECOND_BRAIN_BACKENDS_CONFIG')
    if configured and Path(configured).resolve()!=ROOT/'config/backends.toml':
        raise ValueError('Alternate role configuration is outside the reviewed manifest')
    from src.embeddings import EMBEDDING_MODEL
    if EMBEDDING_MODEL!=manifest['embedding_model']:
        raise ValueError('Embedding model differs from approved destination')
    assert_isolated()
    from src.db import get_connection
    with get_connection() as conn,conn.cursor() as cur:
        from psycopg2 import sql
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'schema_migrations'")
        tables=[row[0] for row in cur.fetchall()]
        for table in tables:
            cur.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM {})').format(sql.Identifier('public',table)))
            if cur.fetchone()[0]:raise ValueError('Trial database must start empty; unrelated records could escape')
    output=Path(output);output.mkdir(parents=True,exist_ok=False,mode=0o700)
    manifest_path=output/'manifest.json';manifest_path.write_text(json.dumps(manifest,indent=2)+'\n');manifest_path.chmod(0o600)
    stages=[]
    def record(name, operation):
        start=datetime.now(timezone.utc).isoformat()
        try:result=operation()
        except Exception:
            stages.append({'stage':name,'started':start,'finished':datetime.now(timezone.utc).isoformat(),'status':'failed'})
            (output/'stages.json').write_text(json.dumps(stages,indent=2)+'\n')
            raise
        stages.append({'stage':name,'started':start,'finished':datetime.now(timezone.utc).isoformat(),'status':'completed'})
        (output/'stages.json').write_text(json.dumps(stages,indent=2)+'\n')
        return result
    from src.capture.framework import run_records
    from src.embeddings import generate_embedding
    from src.search import hybrid_search,rerank
    from src.dream_cycle.orchestrator import DreamCycleOrchestrator
    from src.express import compose_briefing,edit_briefing,render_markdown
    records=[dict(r,type='source',metadata={'trial_source_sha256':r['sha256'],'project':r['project']}) for r in manifest['records']]
    def ingest():
        result=run_records(records)
        if sum(v['failed'] for v in result.values()) or sum(v['processed'] for v in result.values())!=len(records):
            raise RuntimeError('Frozen source ingestion incomplete')
        return result
    record('normalized_source_ingestion',ingest)
    baseline=record('ordinary_retrieval',lambda:rerank(hybrid_search(manifest['task'],generate_embedding(manifest['task']),limit=10),manifest['task']))
    baseline=[{k:str(r[k]) if k=='id' else r.get(k) for k in ('id','title','content','source_url','project')} for r in baseline]
    (output/'baseline.json').write_text(json.dumps(baseline,indent=2,default=str)+'\n')
    previous={name:os.environ.get(name) for name in ('SECOND_BRAIN_PROFILE','SECOND_BRAIN_RUN_COST_CAP_USD','SECOND_BRAIN_DIGEST_DIR')}
    try:
        os.environ['SECOND_BRAIN_DIGEST_DIR']=str(output/'digests')
        os.environ['SECOND_BRAIN_PROFILE']='controlled';os.environ['SECOND_BRAIN_RUN_COST_CAP_USD']=str(manifest['reasoning_stop_threshold_usd'])
        result=record('native_discovery',lambda:DreamCycleOrchestrator().run('user_triggered',scope={'task':manifest['task']},capture_dir=str(output/'calls')))
        (output/'run.json').write_text(json.dumps(asdict(result),indent=2,default=str)+'\n')
        briefing=record('local_briefing',lambda:render_markdown(edit_briefing(compose_briefing(),use_llm=False)))
        (output/'briefing.md').write_text(briefing)
    finally:
        for name,value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value
    candidates=[];run_stats={"no_run_record": True}
    if result.run_id:
        with get_connection() as conn,conn.cursor() as cur:
            cur.execute('SELECT candidate_json,final_verdict,created_memory_id FROM dream_cycle_candidates WHERE run_id=%s ORDER BY created_at,id',(result.run_id,))
            candidates=[{'candidate':r[0],'verdict':r[1],'memory_id':str(r[2]) if r[2] else None} for r in cur.fetchall()]
            cur.execute('SELECT stats FROM dream_cycle_runs WHERE id=%s',(result.run_id,))
            run_stats=cur.fetchone()[0]
    (output/'candidates.json').write_text(json.dumps(candidates,indent=2,default=str)+'\n')
    accepted=[c['candidate'] for c in candidates if c['verdict']=='ACCEPTED']
    arms=[('retrieval',baseline),('discovery',accepted)];random.SystemRandom().shuffle(arms)
    (output/'blind-packets.json').write_text(json.dumps({chr(65+i):v for i,(_,v) in enumerate(arms)},indent=2,default=str)+'\n')
    (output/'private-arm-key.json').write_text(json.dumps({chr(65+i):k for i,(k,_) in enumerate(arms)},indent=2)+'\n')
    report={'fixture_models':fixture,'native_acquisition_and_distillation_exercised':False,'aborted':result.aborted_early,
            'stage_counts':run_stats,'candidate_count':len(candidates),'accepted_count':len(accepted),'known_reasoning_usd':result.cost_usd,
            'spend_incomplete':result.spend_unknown,'embedding_cost_usd':None,'human_review':None,'outcome':None,
            'omitted_stages':manifest['omitted_stages']}
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    for p in output.rglob('*'):
        if p.is_file():p.chmod(0o600)
    return report


def record_review(directory, *, label, ratings, evidence, action=None, outcome=None):
    """Only explicit human submissions fill judgments; unknown stays null."""
    directory=Path(directory)
    labels=json.loads((directory/'blind-packets.json').read_text())
    if label not in labels:raise ValueError('Unknown blind arm')
    if set(ratings)!=set(DIMENSIONS):raise ValueError('Supply all five ratings, using null for unknown')
    for value in ratings.values():
        if value is not None and (isinstance(value,bool) or not isinstance(value,int) or value not in range(5)):
            raise ValueError('Ratings are 0–4 integers or null')
    if not evidence.strip():raise ValueError('Human review requires supporting observations')
    entry={'label':label,'ratings':ratings,'evidence':evidence,'action':action,'outcome':outcome,'at':datetime.now(timezone.utc).isoformat()}
    with (directory/'human-reviews.jsonl').open('a') as f:f.write(json.dumps(entry)+'\n')
    (directory/'human-reviews.jsonl').chmod(0o600)
    return entry
