"""SQL retrieval legs. Fusion, scoring and deduplication stay pure."""
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from .pool import get_connection
from .columns import scalar_columns
from src.retrieval import build_filters,tokenize_query,fts_passes

#: The lexical OR pass leaves out a query term when every lexeme it produces is in
#: more than this fraction of rows, per the planner statistics ANALYZE keeps for
#: ``memories.search_vector`` (pg_stats.most_common_elems). On the live corpus
#: (171k rows) an OR of the full question ("file" 62 %, "get" 29 %, "3" 43 %)
#: matched 76 % of rows and ranking them took ~3 s of every unfiltered search (M-5).
OR_PASS_MAX_LEXEME_FREQ = 0.05

_COMMON_TERMS = sql.SQL("""SELECT t.term FROM unnest(%s::text[]) AS t(term)
    CROSS JOIN LATERAL unnest(tsvector_to_array(to_tsvector('english', t.term))) AS l(lexeme)
    LEFT JOIN (SELECT unnest(most_common_elems::text::text[]) AS lexeme, unnest(most_common_elem_freqs) AS freq
               FROM pg_stats WHERE schemaname = current_schema() AND tablename = 'memories'
               AND attname = 'search_vector') s ON s.lexeme = l.lexeme
    GROUP BY t.term HAVING bool_and(coalesce(s.freq, 0) > %s)""")


def common_terms(cur,terms):
    """Terms whose lexemes are all above :data:`OR_PASS_MAX_LEXEME_FREQ`. Without
    statistics (a never-analyzed table) nothing is common."""
    if len(terms)<2:return set()
    cur.execute(_COMMON_TERMS,(list(terms),OR_PASS_MAX_LEXEME_FREQ))
    return {row['term'] for row in cur.fetchall()}


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
        terms=list(dict.fromkeys(tokenize_query(query_text)))
        for query in fts_passes(terms,common_terms(cur,terms)):
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
