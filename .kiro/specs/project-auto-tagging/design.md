# Project attribution: design

Parsers extract workspace context; renderers emit optional Project headers. ingest_content resolves explicit parameters before headers and calls normalize_project_tag. The normalizer standardizes strings; it does not establish that a value is trustworthy attribution.

Native Codex snapshots preserve explicit assignment metadata. Maintenance inventories historical UUID tags and clears reviewed matches under row checks while retaining legacy_uuid_project.

Code authority: [src/project.py](../../../src/project.py), [src/parsers/ide_chat.py](../../../src/parsers/ide_chat.py), [src/parsers/kiro_sessions.py](../../../src/parsers/kiro_sessions.py), [src/capture/sources/codex.py](../../../src/capture/sources/codex.py), [src/ingest.py](../../../src/ingest.py), [src/db/maintenance.py](../../../src/db/maintenance.py).

Regression authority: [tests/test_project.py](../../../tests/test_project.py), [tests/test_project_properties.py](../../../tests/test_project_properties.py), [tests/test_capture_framework.py](../../../tests/test_capture_framework.py), [tests/test_data_integrity.py](../../../tests/test_data_integrity.py). Test presence is not a passing result.
