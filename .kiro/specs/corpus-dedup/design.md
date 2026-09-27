# Source identity and resumable repair: design

plan_dedup identifies provable duplicates. apply_dedup verifies manifest/target and remaps references before committing each operation; UUID remapping never substring-rewrites captured prose.

After metadata/dedup repair, plan_rechunk freezes complete chunk sequences and input estimates. apply_rechunk reuses validated vectors, re-locks parent/active children and commits replacement plus progress together. Drift or storage failure preserves prior state.

Code authority: [src/db/maintenance.py](../../../src/db/maintenance.py), [src/ingest.py](../../../src/ingest.py), [scripts/maintenance/dedup_corpus.py](../../../scripts/maintenance/dedup_corpus.py), [scripts/maintenance/rechunk_and_embed.py](../../../scripts/maintenance/rechunk_and_embed.py).

Regression authority: [tests/test_data_integrity.py](../../../tests/test_data_integrity.py), [tests/test_release_qualification.py](../../../tests/test_release_qualification.py). Test presence is not a passing result.
