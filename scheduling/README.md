# macOS schedule templates

These plists describe proposed schedules for the repaired installation. They do not establish which jobs are loaded or healthy on a machine. Installed jobs can read changed source immediately, so editing their checkout is deployment.

Read the [generated schedule inventory](../docs/generated/runtime-inventory.md) for labels, commands and timing. Regenerate it after changing a plist with `python scripts/docs/inventory.py`. Use the [Operations release sequence](../docs/OPERATIONS.md) before installing or reloading any job; retain the observed enablement of each job instead of loading every template.

The shared [job wrapper](../scripts/jobs/job_wrapper.sh) supplies the repository environment, exclusive job lock, private log and durable JSON status under `logs/jobs/`. Batch jobs have finite deadlines. The capture API uses bounded startup and recurring health probes; a fresh successful heartbeat keeps the service running, while failed probes, process exit or interruption produce failure status and bounded process-group cleanup.

The retained `chat-extract` label now invokes direct chat capture, including native IDE sessions. The `ingest` label handles the Crawlee source store. Scheduled chat staging and the Quick Desktop graph writer are retired; existing staged files remain available to the explicit maintenance drain utility. These source changes do not unload installed jobs or remove historical records.

Check channel output and durable job status as described in [Operations](../docs/OPERATIONS.md). A launchd exit code or running PID alone does not demonstrate successful capture or useful discoveries.
