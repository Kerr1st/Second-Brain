"""Offline ground-truth retrieval gate. Synthetic relevance is not human benefit."""
import json
from pathlib import Path

from src import db
from src.search import hybrid_search

FIXTURE=json.loads((Path(__file__).parent/'fixtures/retrieval/synthetic_36.json').read_text())


def kendall_tau(expected,actual):
    # Missing targets count as wrong rather than disappearing from the metric.
    if set(expected)!=set(actual):return -1.0
    positions={mid:i for i,mid in enumerate(actual)}
    pairs=[positions[a]<positions[b] for i,a in enumerate(expected) for b in expected[i+1:]]
    return (2*sum(pairs)-len(pairs))/len(pairs) if pairs else 1.0


def test_committed_queries_hit_mrr_distinctness_and_order(test_db,clean_tables):
    with db.transaction() as conn,conn.cursor() as cur:
        for row in FIXTURE['rows']:
            cur.execute("INSERT INTO memories(id,type,title,content,embedding) VALUES(%s,'source',%s,%s,%s)",(row['id'],row['title'],row['content'],row['embedding']))
    reciprocal=[];hits=[];taus=[]
    for query in FIXTURE['queries']:
        rows=hybrid_search(query['text'],query['embedding'],limit=3)
        ids=list(map(lambda r:str(r['id']),rows))
        assert len(ids)==len(set(ids))==3
        target=query['expected'][0]
        hits.append(target in ids)
        reciprocal.append(1/(ids.index(target)+1) if target in ids else 0)
        taus.append(kendall_tau(query['expected'],ids))
    assert len(hits)==36
    assert sum(hits)/len(hits)==1.0
    assert sum(reciprocal)/len(reciprocal)>=0.98 # baseline MRR1.0, maximum degradation.02
    assert min(taus)>=0.8


def test_unembedded_parent_is_fulltext_reachable(test_db,clean_tables):
    parent=db.create_memory('source','Project amber','Uniqueamber planning evidence',source_type='article',source_url='test://parent')
    db.create_memory('source','Vector distraction','Different project',embedding=FIXTURE['queries'][0]['embedding'])
    rows=hybrid_search('Uniqueamber',FIXTURE['queries'][0]['embedding'],limit=5)
    assert parent in [str(row['id']) for row in rows]
