"""Pure retrieval transformations; JSON rank maps form the regression seam."""
import re
from psycopg2 import sql


def build_filters(type=None,status=None,project=None,source_type=None,created_after=None):
    conditions=[sql.SQL('TRUE')];params=[]
    for field,value in [('type',type),('status',status),('source_type',source_type),('created_at',created_after)]:
        if value is not None:
            conditions.append(sql.SQL('{} {} %s').format(sql.Identifier(field),sql.SQL('>=' if field=='created_at' else '=')))
            params.append(value)
    if project:
        conditions.append(sql.SQL('(project = %s OR project IS NULL)'));params.append(project)
    return sql.SQL('WHERE ')+sql.SQL(' AND ').join(conditions),params


def tokenize_query(text):
    return re.sub(r'[^\w\s]',' ',text).lower().split()


def fts_passes(terms):
    terms=list(dict.fromkeys(terms))
    if not terms:return []
    return [' & '.join(terms),' | '.join(terms)] if len(terms)>1 else [terms[0]]


def rrf_fuse(vec_ranks,fts_ranks,k=60,absent_rank=41):
    scored=[(mid,1/(k+vec_ranks.get(mid,absent_rank))+1/(k+fts_ranks.get(mid,absent_rank)))
            for mid in vec_ranks.keys()|fts_ranks.keys()]
    return sorted(scored,key=lambda item:(-item[1],item[0]))


def normalise_rrf(scored):
    if not scored:return {}
    lo=min(score for _,score in scored);hi=max(score for _,score in scored)
    return {mid:(score-lo)/(hi-lo) if hi>lo else 1.0 for mid,score in scored}


def dedup_candidates(scored,rows_by_id,limit,per_parent_cap=2):
    normalized=normalise_rrf(scored)
    out=[];seen=set();parents={}
    for mid,score in scored:
        if mid not in rows_by_id:continue
        row=dict(rows_by_id[mid])
        # A shared opening paragraph is not proof that two sources are duplicates.
        content=re.sub(r'\s+',' ',row.get('content') or '').strip().casefold()
        key=(content,row.get('project'))
        parent=str(row.get('parent_id') or mid)
        if content and key in seen or parents.get(parent,0)>=per_parent_cap:continue
        seen.add(key);parents[parent]=parents.get(parent,0)+1
        row['rrf_raw']=score;row['rrf_score']=normalized[mid];out.append(row)
        if len(out)>=limit:break
    return out
