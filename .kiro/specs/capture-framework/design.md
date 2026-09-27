# Capture channels and vocabulary: design

Adapters collect source-specific batches. run_channel validates and normalizes documents before ingestion; run_records retains parser-owned metadata and exact bodies using parse_header=False, with refresh explicitly selected by its caller.

Native Kiro sessions use stable URLs and snapshot checks. Ingestion owns atomic parent/chunk storage; optional relationship discovery follows commit. Source failures remain visible instead of becoming empty successful captures.

Code authority: [src/capture/framework.py](../../../src/capture/framework.py), [src/capture/common.py](../../../src/capture/common.py), [src/capture/render.py](../../../src/capture/render.py), [src/capture/window.py](../../../src/capture/window.py), [src/parsers/kiro_sessions.py](../../../src/parsers/kiro_sessions.py), [src/capture/codex.py](../../../src/capture/codex.py), [src/ingest.py](../../../src/ingest.py).

Regression authority: [tests/test_capture_framework.py](../../../tests/test_capture_framework.py), [tests/test_codex_capture.py](../../../tests/test_codex_capture.py), [tests/test_capture_api.py](../../../tests/test_capture_api.py), [tests/test_data_integrity.py](../../../tests/test_data_integrity.py). Test presence is not a passing result.
