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
        # Lexical leg (F-048 / fix 6): reaches rows without an embedding (unembedded
        # leaves, childless parents) but NOT parents that have active chunks -- those
        # are represented by their chunks, and whole documents of up to 1.3 M chars
        # would otherwise flood the leg with rank ~1.0 (review B N2). ts_rank uses
        # normalisation 1|32: divide by 1+log(document length), then scale to 0..1.
        for query in fts_passes(tokenize_query(query_text)):
            cur.execute(sql.SQL("""SELECT id, ts_rank(search_vector,to_tsquery('english',%s),33) AS rank FROM memories m {}
                AND search_vector @@ to_tsquery('english',%s)
                AND NOT EXISTS (SELECT 1 FROM memories c WHERE c.parent_id = m.id AND c.status = 'active')
                ORDER BY rank DESC,id LIMIT %s""").format(where),[query,*params,query,prefetch])
            for row in cur.fetchall():
                mid=str(row['id'])
                if mid not in fts:fts[mid]=len(fts)+1
            if len(fts)>=prefetch:break
        ids=list(vec.keys()|fts.keys())
        if not ids:return vec,fts,{}
        cur.execute(sql.SQL('SELECT {} FROM memories WHERE id=ANY(%s::uuid[])').format(scalar_columns()),(ids,))
        return vec,fts,{str(r['id']):dict(r) for r in cur.fetchall()}
