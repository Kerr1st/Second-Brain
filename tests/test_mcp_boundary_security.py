"""Boundary regressions: failures cannot disclose DB diagnostics or run unbounded calls."""
import uuid
from unittest.mock import patch
import psycopg2
from psycopg2.pool import PoolError
import pytest
from src import mcp_server as server


@pytest.mark.parametrize('error', [psycopg2.OperationalError('password=secret SELECT private'), PoolError('dsn secret'), psycopg2.errors.CheckViolation('private SQL row')])
def test_read_does_not_expose_storage_diagnostics(error):
    with patch.object(server, 'get_memory', side_effect=error):
        result = server.memory_read(str(uuid.uuid4()))
    assert 'error' in result
    assert 'secret' not in str(result) and 'private' not in str(result) and 'SELECT' not in str(result)


def test_list_translates_pool_failure():
    with patch.object(server, 'list_memories', side_effect=PoolError('password secret')):
        with pytest.raises(ValueError, match='temporarily unavailable'):
            server.memory_list()


@pytest.mark.parametrize('value', [True, float('inf'), float('nan'), {}, 'oops'])
def test_activity_rejects_bad_window_before_db(value):
    with patch.object(server, 'get_arrivals_summary') as query:
        assert 'error' in server.memory_recent_activity(days=value)
        query.assert_not_called()


def test_learn_cap_before_paid_embeddings():
    with patch.object(server, 'generate_embedding') as embed:
        assert server.memory_learn('A useful article', 'a,b,c,d,e').startswith('Error:')
        assert server.memory_learn('A useful article', 'a'*201).startswith('Error:')
        embed.assert_not_called()


def test_unknown_source_rejected_before_embedding():
    with patch.object(server, 'generate_embedding') as embed:
        assert 'error' in server.memory_search('test', source_type='unregistered-secret-channel')
        assert server.memory_create('idea', 't', 'c', source_type='bogus').startswith('Error:')
        embed.assert_not_called()


def test_graph_returns_incoming_neighbor(test_db, clean_tables):
    from src.db import create_memory, create_relationship
    left=create_memory(type='idea',title='left',content='c')
    right=create_memory(type='idea',title='right',content='c')
    create_relationship(left,right,'supports')
    graph=server.memory_graph(right)
    assert graph['relationships'][0]['target_id']==left


def test_programming_error_not_hidden():
    with patch.object(server, 'get_memory', side_effect=ZeroDivisionError):
        with pytest.raises(ZeroDivisionError):
            server.memory_read(str(uuid.uuid4()))


def test_read_omits_vectors_and_tsvector(test_db, clean_tables):
    """F-057: memory_read returns content and scalars, never the embedding or search_vector."""
    from src import db
    mid = db.create_memory("idea", "Vectors stay home", "Some content worth reading", embedding=[1.0] + [0.0] * 1023)
    out = server.memory_read(mid)
    assert out["content"] == "Some content worth reading" and out["id"] == mid
    assert "embedding" not in out and "search_vector" not in out
