# Scheduled operation evidence: design

job_wrapper.sh dispatches operations.jobs. A locked run records running state, starts an owned process group and waits for a bounded batch or monitors service health. Cleanup precedes terminal status publication.

operations.liveness reads `config/liveness.toml` (required and advisory channels with reasons, generic zero-output rule, dream-cycle freshness, job status sweep) and the durable status files, and writes `logs/jobs/liveness-report.json`; `verify_liveness.sh` is its wrapper. Jobs flagged `SECOND_BRAIN_JOB_NEEDS_NETWORK=1` wait for DNS before their deadline starts and exit 68 without starting if it never comes. `scripts/jobs/_preflight.sh` exits 69 when PostgreSQL is unreachable instead of starting it.

Code authority: [src/operations/jobs.py](../../../src/operations/jobs.py), [src/operations/liveness.py](../../../src/operations/liveness.py), [scripts/jobs/job_wrapper.sh](../../../scripts/jobs/job_wrapper.sh), [scripts/jobs/verify_liveness.sh](../../../scripts/jobs/verify_liveness.sh), [scripts/jobs/_preflight.sh](../../../scripts/jobs/_preflight.sh), [config/liveness.toml](../../../config/liveness.toml), [scheduling/](../../../scheduling/README.md).

Regression authority: [tests/test_job_runner.py](../../../tests/test_job_runner.py), [tests/test_monitor_jobs.py](../../../tests/test_monitor_jobs.py), [tests/test_liveness_policy.py](../../../tests/test_liveness_policy.py), [tests/test_scheduling_plists.py](../../../tests/test_scheduling_plists.py), [tests/test_job_scripts.py](../../../tests/test_job_scripts.py). Test presence is not a passing result.
