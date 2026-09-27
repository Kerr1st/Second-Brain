# Database lifecycle and SQL ownership: design

src.db is the compatibility facade over focused pool, memory, relationship and query modules. connection_scope uses a supplied transaction or owns a new scope; a semaphore bounds pressure before ThreadedConnectionPool acquisition.

Migration bootstrap and each migration use the same lock. Schema changes and tracking commit together. Runtime role separation is a release operation, not an import-time side effect.

Code authority: [src/db/__init__.py](../../../src/db/__init__.py), [src/db/pool.py](../../../src/db/pool.py), [src/db/memories.py](../../../src/db/memories.py), [migrations/migrate.sh](../../../migrations/migrate.sh).

Regression authority: [tests/test_data_integrity.py](../../../tests/test_data_integrity.py), [tests/test_pool_properties.py](../../../tests/test_pool_properties.py), [tests/test_schema_migration_runner.py](../../../tests/test_schema_migration_runner.py). Test presence is not a passing result.
