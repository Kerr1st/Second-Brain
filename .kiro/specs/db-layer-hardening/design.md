# Database lifecycle and SQL ownership: design

src.db is the compatibility facade over focused pool, memory, relationship and query modules. connection_scope uses a supplied transaction or owns a new scope; a semaphore bounds pressure before ThreadedConnectionPool acquisition.

Migration bootstrap and each migration use the same lock. Schema changes and tracking commit together. 017 creates the owner, reassigns ownership, grants DML and default privileges and verifies them; the demotion of the application role is a separate operator step with an immediate reversal. 018 drops the knowledge-graph tables behind the `graph-export` marker in `corpus_maintenance`.

Code authority: [src/db/__init__.py](../../../src/db/__init__.py), [src/db/pool.py](../../../src/db/pool.py), [src/db/memories.py](../../../src/db/memories.py), [migrations/migrate.sh](../../../migrations/migrate.sh), [migrations/017_role_separation.sql](../../../migrations/017_role_separation.sql), [migrations/018_retire_knowledge_graph.sql](../../../migrations/018_retire_knowledge_graph.sql), [scripts/maintenance/smoke_app_role.py](../../../scripts/maintenance/smoke_app_role.py).

Regression authority: [tests/test_data_integrity.py](../../../tests/test_data_integrity.py), [tests/test_pool_properties.py](../../../tests/test_pool_properties.py), [tests/test_schema_migration_runner.py](../../../tests/test_schema_migration_runner.py), [tests/test_migration_016.py](../../../tests/test_migration_016.py), [tests/test_migration_017.py](../../../tests/test_migration_017.py), [tests/test_migration_018.py](../../../tests/test_migration_018.py). Test presence is not a passing result.
