"""Dry-run planning and transactional corpus repair, never called by normal jobs.

Plans contain identities, IDs and hashes, not corpus text. Application verifies
both database incarnation and row fingerprints and records progress in the same
transaction as each repair. Unknown foreign-key layouts fail closed.
"""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from .pool import get_connection, transaction
from .memories import create_memory, update_fields


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def body_hash(row):
    return hashlib.sha256(row['content'].encode()).hexdigest()


def identity(row, parent=None):
    return [row.get('source_type'), row.get('source_url'), row.get('project'), row.get('type'),
            str(parent if parent is not None else row.get('parent_id') or ''),
            (row.get('metadata') or {}).get('record_kind'),
            (row.get('metadata') or {}).get('chunk_index')]


def database_identity(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), oid, pg_postmaster_start_time()::text FROM pg_database WHERE datname=current_database()")
        return list(cur.fetchone())


def _rows(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, type, title, content, source_type, source_url, project, parent_id, metadata, status, embedding IS NOT NULL AS embedded FROM memories ORDER BY created_at, id")
        return [dict(row) for row in cur.fetchall()]


def plan_dedup():
    """Only identical content under the same captured-source identity is mergeable."""
    with get_connection() as conn:
        rows = _rows(conn)
        plan = {'version': 1, 'kind': 'dedup', 'database': database_identity(conn), 'operations': []}
    canonical = {}
    rows_by_id = {str(row['id']): row for row in rows}
    seen = {}
    # Parents before children means duplicate imported trees can share an identity.
    for row in sorted(rows, key=lambda r: r['parent_id'] is not None):
        if (row['type'] != 'source' or not row['source_url'] or row['source_type'] in
                ('codex_task', 'distilled_agent_task', 'distilled_chat', 'dream_cycle') or
                (row['metadata'] or {}).get('record_kind')):
            continue
        parent = canonical.get(str(row['parent_id']), str(row['parent_id'] or ''))
        # A repeated paragraph at a different chunk position remains distinct evidence.
        key = json.dumps([identity(row, parent), body_hash(row), row['status']], sort_keys=True)
        rid = str(row['id'])
        if key not in seen:
            seen[key] = rid
            canonical[rid] = rid
            continue
        keeper = seen[key]
        canonical[rid] = keeper
        plan['operations'].append({'old_id': rid, 'new_id': keeper, 'content_sha256': body_hash(row),
                                   'identity': identity(row), 'keeper_identity': identity(rows_by_id[keeper]),'status':row['status']})
    # Chunk repairs precede parent repairs, keeping manifest child identities stable.
    plan['operations'].sort(key=lambda op: not bool(op['identity'][4]))
    return plan


def remap_json(value, mapping):
    """Rewrite exact UUID values/keys; never substring-replace captured prose."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [remap_json(item, mapping) for item in value]
    if isinstance(value, dict):
        return {mapping.get(k, k): remap_json(v, mapping) for k, v in value.items()}
    return value


def _check_reference_layout(cur):
    cur.execute("""SELECT c.conrelid::regclass::text, a.attname
        FROM pg_constraint c JOIN unnest(c.conkey) AS k(attnum) ON true
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.contype='f' AND c.confrelid='memories'::regclass""")
    allowed = {('memories', 'parent_id'), ('memory_relationships', 'source_id'),
               ('memory_relationships', 'target_id'), ('dream_cycle_candidates', 'created_memory_id'),
               ('memory_skim', 'memory_id'), ('memory_entities', 'memory_id')}
    unknown = set(cur.fetchall()) - allowed
    if unknown:
        raise ValueError(f"Unrecognized references require a migration policy: {sorted(unknown)}")


def _remap_references(conn, old, new):
    with conn.cursor() as cur:
        _check_reference_layout(cur)
        cur.execute("SELECT source_id::text, target_id::text, relation_type, note, created_at, expired_at FROM memory_relationships WHERE source_id=%s OR target_id=%s FOR UPDATE", (old, old))
        for a, b, relation, note, created, expired in cur.fetchall():
            a, b = (new if a == old else a), (new if b == old else b)
            if a == b:
                continue
            if relation in ('related_to', 'contradicts'):
                a, b = sorted((a, b))
            cur.execute("""INSERT INTO memory_relationships(source_id,target_id,relation_type,note,created_at,expired_at)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,target_id,relation_type) DO UPDATE
                SET note=concat_ws(E'\n', memory_relationships.note, EXCLUDED.note),
                    created_at=LEAST(memory_relationships.created_at, EXCLUDED.created_at),
                    expired_at=CASE WHEN memory_relationships.expired_at IS NULL OR EXCLUDED.expired_at IS NULL THEN NULL
                                    ELSE GREATEST(memory_relationships.expired_at, EXCLUDED.expired_at) END""", (a,b,relation,note,created,expired))
        cur.execute('DELETE FROM memory_relationships WHERE source_id=%s OR target_id=%s', (old,old))
        cur.execute('UPDATE memories SET parent_id=%s WHERE parent_id=%s', (new,old))
        for column in ('created_memory_id', 'target_memory_id'):
            cur.execute(sql.SQL('UPDATE dream_cycle_candidates SET {}=%s WHERE {}=%s').format(sql.Identifier(column),sql.Identifier(column)), (new,old))
        cur.execute("""INSERT INTO memory_entities(memory_id,entity_id,relation,created_at)
            SELECT %s,entity_id,relation,created_at FROM memory_entities WHERE memory_id=%s
            ON CONFLICT DO NOTHING""", (new,old))
        cur.execute('DELETE FROM memory_entities WHERE memory_id=%s', (old,))
        # Retain conflicting skims verbatim in the survivor's metadata before deletion.
        cur.execute("""SELECT row_to_json(s) FROM memory_skim s WHERE s.memory_id=%s AND EXISTS
            (SELECT 1 FROM memory_skim n WHERE n.memory_id=%s AND n.model=s.model AND n.prompt_version=s.prompt_version)""", (old,new))
        archived = [r[0] for r in cur.fetchall()]
        if archived:
            cur.execute("""UPDATE memories SET metadata=COALESCE(metadata,'{}'::jsonb) || jsonb_build_object(
                'dedup_archived_skims', COALESCE(metadata->'dedup_archived_skims','[]'::jsonb) || %s::jsonb)
                WHERE id=%s""", (json.dumps(archived),new))
            cur.execute("""DELETE FROM memory_skim s WHERE s.memory_id=%s AND EXISTS
                (SELECT 1 FROM memory_skim n WHERE n.memory_id=%s AND n.model=s.model AND n.prompt_version=s.prompt_version)""", (old,new))
        cur.execute('UPDATE memory_skim SET memory_id=%s WHERE memory_id=%s', (new,old))
        # Express feedback has untyped UUID / UUID:UUID item keys, not FKs.
        cur.execute("SELECT id::text,target_key,signal,weight,updated_at FROM express_feedback WHERE target_type='item' AND target_key LIKE %s FOR UPDATE", ('%'+old+'%',))
        for fid, target, signal, weight, updated in cur.fetchall():
            mapped=':'.join(new if part==old else part for part in target.split(':'))
            if mapped==target: continue
            cur.execute("SELECT id::text,signal,weight,updated_at FROM express_feedback WHERE target_type='item' AND target_key=%s FOR UPDATE", (mapped,))
            conflict=cur.fetchone()
            if conflict:
                cur.execute("""UPDATE memories SET metadata=COALESCE(metadata,'{}'::jsonb) || jsonb_build_object(
                    'dedup_archived_feedback',COALESCE(metadata->'dedup_archived_feedback','[]'::jsonb) || %s::jsonb) WHERE id=%s""",
                    (json.dumps([{'id':fid,'target_key':target,'signal':signal,'weight':weight,'updated_at':str(updated)},
                                 {'id':conflict[0],'target_key':mapped,'signal':conflict[1],'weight':conflict[2],'updated_at':str(conflict[3])}]),new))
                if updated>conflict[3]:
                    cur.execute('UPDATE express_feedback SET signal=%s,weight=%s,updated_at=%s WHERE id=%s',(signal,weight,updated,conflict[0]))
                cur.execute('DELETE FROM express_feedback WHERE id=%s',(fid,))
            else:
                cur.execute('UPDATE express_feedback SET target_key=%s WHERE id=%s',(mapped,fid))
        # Enumerate all JSONB columns, not just candidate_json. This includes run
        # ledgers, future evidence objects and metadata-based lineage references.
        cur.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND data_type='jsonb'")
        for table, column in cur.fetchall():
            query=sql.SQL('SELECT ctid::text, {} FROM {} WHERE {}::text LIKE %s FOR UPDATE').format(sql.Identifier(column),sql.Identifier(table),sql.Identifier(column))
            cur.execute(query, ('%'+old+'%',))
            for ctid, value in cur.fetchall():
                changed=remap_json(value,{old:new})
                if changed != value:
                    cur.execute(sql.SQL('UPDATE {} SET {}=%s::jsonb WHERE ctid=%s::tid').format(sql.Identifier(table),sql.Identifier(column)), (json.dumps(changed),ctid))
        cur.execute("SELECT table_name,column_name,udt_name FROM information_schema.columns WHERE table_schema='public' AND udt_name IN ('_text','_uuid')")
        for table, column, typ in cur.fetchall():
            cast=sql.SQL('uuid') if typ=='_uuid' else sql.SQL('text')
            cur.execute(sql.SQL('UPDATE {} SET {}=array_replace({}, %s::{}, %s::{}) WHERE %s::{}=ANY({})').format(sql.Identifier(table),sql.Identifier(column),sql.Identifier(column),cast,cast,cast,sql.Identifier(column)), (old,new,old))


def apply_dedup(plan, expected_sha256):
    if plan.get('kind') != 'dedup' or digest(plan) != expected_sha256:
        raise ValueError('Manifest hash/kind does not match')
    completed = 0
    for op in plan['operations']:
        key = op['old_id']
        with transaction() as conn:
            if database_identity(conn) != plan['database']:
                raise ValueError('Manifest database incarnation differs; regenerate the dry run')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('corpus-maintenance',0))")
                cur.execute('SELECT 1 FROM corpus_maintenance WHERE manifest_sha256=%s AND operation_key=%s', (expected_sha256,key))
                if cur.fetchone():
                    continue
                cur.execute('SELECT * FROM memories WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE', ([key,op['new_id']],))
                rows={str(r['id']):dict(r) for r in cur.fetchall()}
                old,new=rows.get(key),rows.get(op['new_id'])
                if not old or not new or any(body_hash(r)!=op['content_sha256'] for r in (old,new)):
                    raise ValueError('Memory content changed since manifest creation')
                if old['status'] != op['status'] or new['status'] != op['status']:
                    raise ValueError('Memory status changed since manifest creation')
                if identity(old)!=op['identity'] or identity(new)!=op['keeper_identity']:
                    raise ValueError('Memory identity changed since manifest creation')
            _remap_references(conn,key,op['new_id'])
            with conn.cursor() as cur:
                # Preserve all original metadata and source identity, even conflicting
                # importer fields, in an auditable alias history on the survivor.
                archive={k:str(v) if k in ('id','parent_id','created_at','updated_at') and v is not None else v
                         for k,v in old.items() if k not in ('embedding','search_vector','content')}
                cur.execute("""UPDATE memories SET metadata=COALESCE(metadata,'{}'::jsonb) || jsonb_build_object(
                    'dedup_aliases', COALESCE(metadata->'dedup_aliases','[]'::jsonb) || %s::jsonb), updated_at=now() WHERE id=%s""", (json.dumps([archive],default=str),op['new_id']))
                cur.execute('DELETE FROM memories WHERE id=%s', (key,))
                cur.execute('INSERT INTO corpus_maintenance(manifest_sha256,operation_key,details) VALUES(%s,%s,%s::jsonb)', (expected_sha256,key,json.dumps({'new_id':op['new_id']})))
        completed += 1
    return completed


def write_manifest(plan, path):
    path=Path(path)
    # Exclusive creation prevents quietly overwriting the approved manifest.
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(plan, indent=2, default=str)+'\n')
    return digest(plan)

def _repair_text(row, strip_known_ide_context=False):
    text = row['content']
    if strip_known_ide_context and row['source_type'] == 'kiro_ide_chat':
        import re
        for tag in ('EnvironmentContext', 'implicit-rules', 'implicitInstruction'):
            text = re.sub(r'<' + tag + r'\b[^>]*>.*?</' + tag + r'>', '', text, flags=re.DOTALL)
    return text


def plan_rechunk(max_chunk_chars=4000, *, strip_known_ide_context=False):
    """Plan bounded replacements and missing leaf vectors without making model calls."""
    from src.ingest import chunk_by_section
    with get_connection() as conn:
        rows=_rows(conn)
        plan={'version':1,'kind':'rechunk','database':database_identity(conn),
              'embedding_model':__import__('os').environ.get('EMBEDDING_MODEL','amazon.titan-embed-text-v2:0'),
              'embedding_dimensions':1024,'strip_known_ide_context':strip_known_ide_context,
              'pricing_basis':'Current corpus; regenerate after dedup before paid application', 'max_chunk_chars':max_chunk_chars,'operations':[],'embedding_calls':0,'input_characters':0}
    children=defaultdict(list)
    for row in rows:
        if row['parent_id'] and row['status']=='active':
            children[str(row['parent_id'])].append(row)
    for row in rows:
        if row['parent_id'] or row['status']!='active' or row['source_type'] in ('codex_task','distilled_agent_task'):
            continue
        rid=str(row['id']); existing=children[rid]
        repair_text=_repair_text(row, strip_known_ide_context)
        replace=bool(existing) or row['type']=='source' or bool((row['metadata'] or {}).get('capture_identity'))
        chunks=chunk_by_section(repair_text,max_chunk_chars) if replace or len(repair_text)>max_chunk_chars else [repair_text]
        chunks=[chunk for chunk in chunks if chunk.strip()]
        if replace:
            # A valid embedded prefix does not prove the complete source was indexed.
            # Verify the whole sequence and its indices against the frozen parent.
            expected={i: text for i,text in enumerate(chunks)}
            actual={}
            complete=len(existing)==len(chunks)
            for child in existing:
                index=(child['metadata'] or {}).get('chunk_index')
                if type(index) is not int:
                    complete=False
                    continue
                if index in actual:
                    complete=False
                actual[index]=child['content']
            needs=not complete or actual!=expected or any(not child['embedded'] for child in existing)
        else:
            needs=not row['embedded'] or len(row['content'])>50000
        if not needs:
            continue
        if not chunks: continue
        # Large standalone leaves receive child vectors but retain their original ID/content.
        mode='chunks' if replace or len(chunks)>1 else 'leaf'
        plan['operations'].append({'memory_id':rid,'content_sha256':body_hash(row),'identity':identity(row),
            'status':row['status'],'mode':mode,'chunk_hashes':[hashlib.sha256(c.encode()).hexdigest() for c in chunks],
            'active_children':[str(r['id']) for r in existing]})
        plan['embedding_calls']+=len(chunks); plan['input_characters']+=sum(map(len,chunks))
    return plan


def apply_rechunk(plan, expected_sha256, *, embed, cache_dir):
    """Apply a reviewed manifest with a caller-supplied, explicitly authorized embedder.

    Completed vectors are cached by content hash before database writes, so a
    storage retry never repays for them. Existing cited chunk IDs are superseded,
    not destroyed. The supplied embedder is the only possible provider seam.
    """
    from src.ingest import chunk_by_section
    from src.classify import classify_memory
    if plan.get('kind')!='rechunk' or digest(plan)!=expected_sha256:
        raise ValueError('Manifest hash/kind does not match')
    cache=Path(cache_dir);cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    if plan_dedup()['operations']:
        raise ValueError('Source duplicates remain; qualify dedup and regenerate the rechunk manifest first')
    applied=0
    for op in plan['operations']:
        rid=op['memory_id']
        with get_connection() as conn:
            if database_identity(conn)!=plan['database']:
                raise ValueError('Manifest database incarnation differs')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT 1 FROM corpus_maintenance WHERE manifest_sha256=%s AND operation_key=%s',(expected_sha256,rid))
                if cur.fetchone(): continue
                cur.execute('SELECT * FROM memories WHERE id=%s',(rid,));row=cur.fetchone()
                if not row or body_hash(row)!=op['content_sha256'] or identity(row)!=op['identity'] or row['status']!=op.get('status'):
                    raise ValueError('Memory changed since manifest creation')
        repair_text=_repair_text(row, plan.get('strip_known_ide_context',False))
        chunks=chunk_by_section(repair_text,plan['max_chunk_chars']) if op['mode']=='chunks' else [repair_text]
        chunks=[c for c in chunks if c.strip()]
        if [hashlib.sha256(c.encode()).hexdigest() for c in chunks]!=op['chunk_hashes']:
            raise ValueError('Chunk algorithm changed since manifest creation')
        vectors=[]
        for text, hashed in zip(chunks,op['chunk_hashes']):
            path=cache/(expected_sha256+'-'+hashed+'.json')
            cached=path.exists()
            vector=json.loads(path.read_text()) if cached else embed(text)
            import math
            if not isinstance(vector,list) or len(vector)!=1024 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vector):
                raise ValueError('Invalid prepared embedding')
            if not cached:
                # Only valid vectors enter the resumable cache; provider errors cannot poison it.
                import os, tempfile
                fd,tmp=tempfile.mkstemp(dir=cache,prefix='.embedding-')
                try:
                    with os.fdopen(fd,'w') as f: json.dump(vector,f,allow_nan=False)
                    os.replace(tmp,path)
                finally:
                    if os.path.exists(tmp): os.unlink(tmp)
            vectors.append(vector)
        with transaction() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('corpus-maintenance',0))")
                cur.execute('SELECT 1 FROM corpus_maintenance WHERE manifest_sha256=%s AND operation_key=%s',(expected_sha256,rid))
                if cur.fetchone(): continue
                cur.execute('SELECT * FROM memories WHERE id=%s FOR UPDATE',(rid,));current=cur.fetchone()
                if not current or body_hash(current)!=op['content_sha256'] or identity(current)!=op['identity'] or current['status']!=op.get('status'):
                    raise ValueError('Memory changed during embedding preparation')
                cur.execute("SELECT id::text FROM memories WHERE parent_id=%s AND status='active' ORDER BY id FOR UPDATE",(rid,))
                if sorted(r['id'] for r in cur.fetchall())!=sorted(op['active_children']):
                    raise ValueError('Active chunk set changed since planning')
                row=current  # Preserve current metadata/title after paid preparation.
                if op['mode']=='leaf': update_fields(rid,{'embedding':vectors[0], 'mem_class':row['mem_class'] or classify_memory(row['type'],row['content'])},conn=conn)
                else:
                    cur.execute("UPDATE memories SET status='superseded',updated_at=now() WHERE parent_id=%s AND status='active'",(rid,))
                    for i,(text,vector) in enumerate(zip(chunks,vectors)):
                        create_memory(type=row['type'], title=f"{row['title']} (chunk {i+1}/{len(chunks)})", content=text,
                            embedding=vector,source_url=row['source_url'],source_type=row['source_type'],project=row['project'],
                            parent_id=rid,metadata={**(row['metadata'] or {}),'chunk_index':i,'total_chunks':len(chunks),'maintenance_manifest':expected_sha256},
                            mem_class=classify_memory(row['type'],text),conn=conn)
                cur.execute('INSERT INTO corpus_maintenance(manifest_sha256,operation_key,details) VALUES(%s,%s,%s::jsonb)',(expected_sha256,rid,json.dumps({'mode':op['mode'],'chunks':len(chunks)})))
        applied+=1
    return applied

def plan_metadata_repairs():
    """Inventory UUID project tags and historical counter mismatches, read-only."""
    with get_connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        plan={'version':1,'kind':'metadata-repairs','database':database_identity(conn),'operations':[]}
        cur.execute("""SELECT id::text,project FROM memories WHERE project ~*
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' ORDER BY id""")
        for row in cur.fetchall():plan['operations'].append({'kind':'uuid-project','id':row['id'],'project':row['project']})
        # Restrict the two independently identified historical runs. Broad repair
        # would conflate generated-but-unjudged candidates with ledger corruption.
        cur.execute("""SELECT r.id::text,r.candidates_generated,r.candidates_accepted,r.candidates_rejected,
            count(c.id)::int AS generated,
            count(c.id) FILTER(WHERE c.final_verdict='ACCEPTED')::int AS accepted,
            count(c.id) FILTER(WHERE c.final_verdict='REJECTED')::int AS rejected
            FROM dream_cycle_runs r LEFT JOIN dream_cycle_candidates c ON c.run_id=r.id
            WHERE r.id::text LIKE '3a1263a6%' OR r.id::text LIKE 'bfe245b1%'
            GROUP BY r.id ORDER BY r.id""")
        for row in cur.fetchall():
            if (row['candidates_accepted'],row['candidates_rejected']) != (row['accepted'],row['rejected']):
                plan['operations'].append({'kind':'run-counts',**dict(row)})
        return plan


def apply_metadata_repairs(plan,expected_sha256):
    if plan.get('kind')!='metadata-repairs' or digest(plan)!=expected_sha256:
        raise ValueError('Manifest hash/kind does not match')
    count=0
    for op in plan['operations']:
        key=op['kind']+':'+op['id']
        with transaction() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
            if database_identity(conn)!=plan['database']:raise ValueError('Database incarnation changed')
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('corpus-maintenance',0))")
            cur.execute('SELECT 1 FROM corpus_maintenance WHERE manifest_sha256=%s AND operation_key=%s',(expected_sha256,key))
            if cur.fetchone():continue
            if op['kind']=='uuid-project':
                cur.execute('SELECT project FROM memories WHERE id=%s FOR UPDATE',(op['id'],));row=cur.fetchone()
                if not row or row['project']!=op['project']:raise ValueError('Project changed since planning')
                update_fields(op['id'],{'project':None},metadata_patch={'legacy_uuid_project':op['project'],'project_repair_manifest':expected_sha256},conn=conn)
            elif op['kind']=='run-counts':
                cur.execute('SELECT candidates_generated,candidates_accepted,candidates_rejected FROM dream_cycle_runs WHERE id=%s FOR UPDATE',(op['id'],));row=cur.fetchone()
                if not row or any(row[k]!=op[k] for k in row):raise ValueError('Run counts changed since planning')
                cur.execute("SELECT count(*)::int AS generated,count(*) FILTER(WHERE final_verdict='ACCEPTED')::int AS accepted,count(*) FILTER(WHERE final_verdict='REJECTED')::int AS rejected FROM dream_cycle_candidates WHERE run_id=%s",(op['id'],))
                if any(v!=op[k] for k,v in cur.fetchone().items()):raise ValueError('Candidate ledger changed since planning')
                cur.execute('UPDATE dream_cycle_runs SET candidates_generated=%s,candidates_accepted=%s,candidates_rejected=%s,stats=COALESCE(stats,\'{}\'::jsonb)||%s::jsonb WHERE id=%s',
                    (max(op['candidates_generated'] or 0,op['generated']),op['accepted'],op['rejected'],json.dumps({'counter_repair_manifest':expected_sha256,'prior_counts':dict(row)}),op['id']))
            else:raise ValueError('Unknown metadata repair kind')
            cur.execute('INSERT INTO corpus_maintenance(manifest_sha256,operation_key) VALUES(%s,%s)',(expected_sha256,key))
        count+=1
    return count


def plan_role_and_index_retirement(owner_role='second_brain_owner',migration_role='second_brain_migrator'):
    """Prepare SQL for a separately authorized admin; never execute DDL here."""
    with get_connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT current_user AS name,rolsuper FROM pg_roles WHERE rolname=current_user');application=cur.fetchone()
        app=application['name']
        if len({app,owner_role,migration_role})!=3:raise ValueError('Application, owner and migration roles must be distinct')
        cur.execute("SELECT schemaname,indexrelname,idx_scan,pg_get_indexdef(indexrelid) AS definition FROM pg_stat_user_indexes WHERE schemaname='public' AND idx_scan=0 ORDER BY indexrelname")
        indexes=[dict(row) for row in cur.fetchall()]
        def render(template,*names):return sql.SQL(template).format(*(sql.Identifier(n) for n in names)).as_string(conn)
        statements=[render('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE',owner_role),
                    render('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE',migration_role),
                    render('GRANT {} TO {}',owner_role,migration_role),
                    render('GRANT USAGE ON SCHEMA public TO {}',app),
                    render('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}',app),
                    render('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}',app)]
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        statements += [render('ALTER TABLE public.{} OWNER TO {}',row['tablename'],owner_role) for row in cur.fetchall()]
        cur.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")
        statements += [render('ALTER SEQUENCE public.{} OWNER TO {}',row['sequencename'],owner_role) for row in cur.fetchall()]
        cur.execute("""SELECT p.oid::regprocedure::text AS identity FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')""")
        statements += [sql.SQL('ALTER FUNCTION {} OWNER TO {}').format(sql.SQL(row['identity']),sql.Identifier(owner_role)).as_string(conn) for row in cur.fetchall()]
        statements += [render('GRANT USAGE,CREATE ON SCHEMA public TO {}',owner_role),
                       render('ALTER DATABASE {} OWNER TO {}',database_identity(conn)[0],owner_role)]
        statements += [render('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {}',owner_role,app),
                       render('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {}',owner_role,app),
                       render('ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE',app)]
        return {'version':1,'kind':'role-index-review','database':database_identity(conn),'application_role':app,
                'currently_superuser':application['rolsuper'],'owner_role':owner_role,'migration_role':migration_role,
                'prerequisites':['Admin must inspect existing roles/ownership and authentication before adapting SQL.',
                                 'Backup restore and migrations must be qualified using the separate migrator.',
                                 'Never grant owner membership to the application role.',
                                 'Zero scan count is evidence for review, not automatic authority to drop an index.'],
                'proposed_sql':statements,'unused_indexes':indexes}
