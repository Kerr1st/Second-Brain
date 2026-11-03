# Public history and provenance

The public repository was synchronized on November 3, 2026 from the verified work-computer source snapshot. The application source corresponds to original commit `ea7b825778cfdc9d953162c4701d73b36c111d71`, whose sanitized counterpart is `fde4caf2e893c5b67c94fdb3314d91956da9d13e`. The earlier GitHub lineage and the work-computer lineage remain separate parents of the publication merge; feature, experiment, release and preservation branches remain separate.

## History sanitization

All 505 distinct development commits across those branches are retained, including commits that become empty after artifact removal. Author and committer metadata, dates, messages, and ordered parent relationships are preserved. 497 commit IDs changed because private artifacts or their ancestor trees changed. Original commit IDs used by existing release and audit documents can be translated using [PUBLIC-COMMIT-MAP.tsv](PUBLIC-COMMIT-MAP.tsv).

Historical runtime logs, backup copies, transient state, raw captured Codex fixtures, and machine/account receipts were excluded from the public trees. The original verified snapshot and full bundle remain private. The publication does not include copied credentials, the database dump, raw Claude sessions, or uncommitted retention work.

The latest application files are unchanged; this publication adds only this provenance note and the commit map. Git-history checks and a static privacy audit were performed. Application code, tests, services, jobs, migrations, restores, and provider calls were not run as part of publication. Historical test and deployment records remain dated evidence.

## Existing clones

This publication rewrites affected public branch histories. Start from a fresh clone or deliberately reconcile against the rewritten branches before pushing. Pushing an old branch without reconciliation can reintroduce removed private history. Existing clones and cached GitHub views may still contain previously public objects; rewritten branch tips do not erase those copies.

## Separate local work

The optional Jev branch remains separate and automatic screening remains disabled. The refactor retention patch and untracked test were not committed or integrated by this publication. The private original snapshot preserves their exact bases.
