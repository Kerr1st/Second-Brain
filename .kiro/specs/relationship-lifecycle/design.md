# Relationship provenance and expiry: design

create_relationship validates/canonicalizes then upserts, reviving explicitly recreated links. get_relationships selects each neighbor and collapses reverse pairs only for symmetric types.

expire_stale_relationships applies age/access gates to non-provenance links. Migration 016 repairs premature expiry and protects permanent provenance. The Dream database facade delegates lifecycle operations to this module.

Code authority: [src/db/relationships.py](../../../src/db/relationships.py), [src/dream_cycle_db.py](../../../src/dream_cycle_db.py), [migrations/016_data_integrity.sql](../../../migrations/016_data_integrity.sql).

Regression authority: [tests/test_data_integrity.py](../../../tests/test_data_integrity.py), [tests/test_discovery_integrity.py](../../../tests/test_discovery_integrity.py). Test presence is not a passing result.
