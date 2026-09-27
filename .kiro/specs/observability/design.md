# Scheduled operation evidence: design

job_wrapper.sh dispatches operations.jobs. A locked run records running state, starts an owned process group and waits for a bounded batch or monitors service health. Cleanup precedes terminal status publication.

operations.liveness reads durable status. verify_liveness.sh separately checks source freshness and Dream Cycle completion, retaining retired-channel distinctions. Retirement preparation emits a reviewable inventory without changing launchd.

Code authority: [src/operations/jobs.py](../../../src/operations/jobs.py), [src/operations/liveness.py](../../../src/operations/liveness.py), [scripts/jobs/job_wrapper.sh](../../../scripts/jobs/job_wrapper.sh), [scripts/jobs/verify_liveness.sh](../../../scripts/jobs/verify_liveness.sh), [scripts/maintenance/prepare_operational_retirement.py](../../../scripts/maintenance/prepare_operational_retirement.py).

Regression authority: [tests/test_job_runner.py](../../../tests/test_job_runner.py), [tests/test_monitor_jobs.py](../../../tests/test_monitor_jobs.py), [tests/test_operational_retirement.py](../../../tests/test_operational_retirement.py). Test presence is not a passing result.
