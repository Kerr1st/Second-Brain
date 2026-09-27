"""SQL retrieval legs. Fusion, scoring and deduplication stay pure."""
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from .pool import get_connection
from .columns import scalar_columns
from src.retrieval import build_filters,tokenize_query,fts_passes


def fetch_candidates(query_text,query_embedding,prefetch,**filters):
    where,params=build_filters(**filters)
    if filters.get('status') is None:
        where += sql.SQL(" AND NOT (parent_id IS NOT NULL AND status = 'superseded')")
    with get_connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        if any(v is not None for v in filters.values()):
            cur.execute('SET LOCAL hnsw.iterative_scan = relaxed_order')
        cur.execute(sql.SQL('SELECT id, 1-(embedding <=> %s::vector) AS similarity FROM memories {} AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s').format(where),[str(query_embedding),*params,str(query_embedding),prefetch])
        # relaxed_order scans are not distance ordered; rank the actual distances.
        vectors=sorted(cur.fetchall(),key=lambda r:(-r['similarity'],str(r['id'])))
        vec={str(r['id']):i+1 for i,r in enumerate(vectors)}
        fts={}
        for query in fts_passes(tokenize_query(query_text)):
            cur.execute(sql.SQL("SELECT id, ts_rank(search_vector,to_tsquery('english',%s)) AS rank FROM memories {} AND search_vector @@ to_tsquery('english',%s) ORDER BY rank DESC,id LIMIT %s").format(where),[query,*params,query,prefetch])
            for row in cur.fetchall():
                mid=str(row['id'])
                if mid not in fts:fts[mid]=len(fts)+1
            if len(fts)>=prefetch:break
        ids=list(vec.keys()|fts.keys())
        if not ids:return vec,fts,{}
        cur.execute(sql.SQL('SELECT {} FROM memories WHERE id=ANY(%s::uuid[])').format(scalar_columns()),(ids,))
        return vec,fts,{str(r['id']):dict(r) for r in cur.fetchall()}
