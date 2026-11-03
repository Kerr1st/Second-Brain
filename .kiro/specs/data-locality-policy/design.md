# Private source boundaries: design

Agent captures, digests and raw debug dumps stay in private output directories (the trial-manifest runner of the refactor branch is not in the release line). Corpus calls share route and metering boundaries.

backup_bundle stages the database dump and selected regular files, records hashes/modes, encrypts and decrypt-verifies locally, then optionally uploads/readbacks ciphertext. Full restore uses an owned socket-only cluster and has a separate verification outcome.

Code authority: [src/backends/claude_code.py](../../../src/backends/claude_code.py), [src/dream_cycle/digest.py](../../../src/dream_cycle/digest.py), [scripts/maintenance/backup_bundle.py](../../../scripts/maintenance/backup_bundle.py).

Regression authority: [tests/test_backup_bundle.py](../../../tests/test_backup_bundle.py), [tests/test_corpus_routing_guard.py](../../../tests/test_corpus_routing_guard.py). Test presence is not a passing result.
