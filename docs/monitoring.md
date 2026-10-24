# Remote run monitoring and cancellation

Relay's local backend now monitors Bitbucket runs whose persisted plans have a
pipeline UUID. Existing submissions from the execution API are adopted at startup,
including manually reconciled plans. Monitoring only reads the remote pipeline and
its steps; restart never resubmits a pipeline.

## Setup and API

Use the same `RELAY_DATA` directory across restarts (default `.relay`). The database
is `remote.sqlite3`. The additive migration creates `remote_runs`, its polling index,
and `schema_migrations` without rewriting plans or local demo run files. The run ID
is the plan ID; project snapshots remain in the plans table. Step snapshots and
artifact-reference placeholders are persisted in each remote run record.

The Bitbucket token requires `read:pipeline:bitbucket` for status and steps, and
`write:pipeline:bitbucket` for cancellation. The existing trigger scopes still apply.
Monitoring starts with the FastAPI lifespan under Uvicorn. Keep the backend running.

- `GET /api/remote-runs?limit=50&offset=0`: newest adopted runs, max 100 per page.
- `GET /api/remote-runs/{plan_id}`: saved observation; no remote request.
- `POST /api/remote-runs/{plan_id}/refresh`: request a fresh observation and resume
  a monitoring window that timed out. An active polling lease returns saved data.
- `POST /api/remote-runs/{plan_id}/cancel` with `{"confirmed":true}`: request a stop
  for that run. No caller-supplied repository or pipeline UUID is accepted.

The legacy local runner remains under `/api/runs`. Plans without a confirmed
pipeline UUID do not appear in remote history; use the plan recovery workflow in
[execution.md](execution.md) first.

## Status semantics

`pipeline_status` is distinct from `test_status`, which remains `not_collected`
until artifact parsing is implemented. `FAILED` does not prove that a test failed;
`ERROR` is retained as the remote result, without inventing a root cause.

| Remote observation | pipeline_status |
| --- | --- |
| PENDING / HALTED / PAUSED | pending |
| IN_PROGRESS | running |
| COMPLETED + SUCCESSFUL | successful |
| COMPLETED + FAILED / ERROR / EXPIRED | failed |
| COMPLETED + STOPPED | stopped |
| Unrecognized state/result | unknown |

`remote_state`, `result`, `terminal`, remote timestamps, step states and names are
stored. Source commands, variables, raw provider errors and arbitrary remote links
are not copied into history. Steps pagination is limited to the same API origin
and exact pipeline steps path, at most 20 pages / 1,000 steps. Each response uses
the existing 2 MB limit. Redirects are rejected.

`monitoring_status` is `active`, `retrying`, `complete` or `timed_out`. On a read
failure, the last valid observation remains visible with its `last_checked_at` and
a sanitized `error`; `last_attempt_at` identifies the failed attempt. An incomplete
steps response is not presented as a complete successful refresh.

Polling runs every 10 seconds per run, with exponential read-error backoff from
20 seconds to a 300-second maximum. Four due runs are processed per cycle. SQLite
leases last 90 seconds and fence stale worker writes; network operations are bounded
below that lease duration. Multiple workers must share the same local database.
After an interrupted worker, polling resumes once its lease expires. Monitoring
stops after a 24-hour window or remote completion. A monitoring timeout does NOT
change the remote pipeline to failed or stopped; explicit Refresh resumes polling.

## Cancellation

A stop request is durably recorded before one POST to Bitbucket. Repeated calls
return that intent instead of issuing another POST, including after restart:

- `requested`: POST in progress; after interruption, polling turns it into `unknown`.
- `acknowledged`: Bitbucket accepted the stop signal (HTTP 204), not yet stopped.
- `unknown`: transport outcome uncertain; polling continues, with no automatic POST retry.
- `rejected`: explicit provider rejection; diagnostic retained, polling continues.
- `confirmed`: subsequent GET observed the pipeline stopped.
- `not_stopped`: subsequent GET observed another terminal outcome, e.g. it completed
  successfully before the stop took effect.

The remote terminal observation wins if it arrives while the cancellation POST is
in flight. Rejected/unknown cancellations are not automatically resent; inspect
Bitbucket and use its UI if another stop attempt is necessary. No stop is sent for
a run already observed terminal. A successful stop signal alone never claims tests
passed, failed, or that the pipeline has stopped.

## Live acceptance

1. Update the branch and restart with the original data directory and credentials.
2. Fetch `/api/remote-runs/{existing_plan_id}` and Refresh; compare pipeline and step
   statuses with Bitbucket, then restart and check that the same UUID is retained.
3. For a separately approved long-running sandbox pipeline, call Cancel once and
   watch the saved status transition from requested/acknowledged to confirmed only
   after the remote result is STOPPED. Repeated Cancel must not create more POSTs.
4. Check that expired credentials/network failure show a monitoring error rather
   than fabricated test results.

This PR implements local monitoring for issue #4. PostgreSQL deployment migrations,
normalized artifact storage/parsers, UI controls and hosted multi-user operation
remain follow-up work. Do not expose this unauthenticated local API publicly.

Reference: https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pipelines/
