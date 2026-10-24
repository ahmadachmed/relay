# Confirmed Bitbucket execution

The remote API is separate from the legacy local demo runner. Preview requires
`read:repository:bitbucket`. Trigger also requires `write:pipeline:bitbucket`;
manual reconciliation requires `read:pipeline:bitbucket`. Keep credentials in the
backend environment. Use a sandbox repository for acceptance testing.

## Preview and review

`POST /api/plans/preview` accepts the same selection as `/api/plans/validate`:

```json
{"project_id":"api","pipeline_id":"smoke","parameters":{"ENV":"uat"}}
```

The backend validates configured branches/parameters, applies defaults, checks the
selected branch's catalog and stores an immutable project snapshot and commit.
The returned `id` is the request ID for execution and deduplication. Review the
project, repository, branch, commit, selector and all parameters before confirming.
Preview sends GET requests only. It expires after 15 minutes.

`POST /api/plans/{id}/execute` accepts only `{"confirmed":true}`. It cannot change
parameters, target or commit; changes require a fresh preview and confirmation.
A changed backend registry invalidates an unused preview. Registry changes take
effect after backend restart. The POST to Bitbucket pins the reviewed commit and
custom selector. A successful response stores only the pipeline UUID and a URL
constructed from the configured repository and verified build number.

## Duplicate requests and recovery

SQLite at `RELAY_DATA/remote.sqlite3` stores plans and execution outcomes separately
from local demo run files. Each plan can issue at most one trigger POST, including
across workers or restarts sharing this database. Repeat Execute using the SAME
plan ID returns its stored status. This does not deduplicate two separate previews;
a new preview represents a new execution intent. Keep this database when restarting.

`GET /api/plans/{id}` returns the plan and submission outcome:

- `preview`: not submitted, awaiting confirmation.
- `submitted`: a pipeline UUID is recorded; this does not mean tests passed.
- `rejected`: Bitbucket explicitly rejected the request. Correct the issue and
  create/review a fresh plan if another attempt is desired.
- `unknown`: POST is in progress, timed out, returned an ambiguous response, or the
  process exited around submission. The same plan will NEVER submit again.

An unknown result is persisted before sending POST. Do not create a replacement
plan until you have inspected Bitbucket for a possible existing run. There are no
background retries. A process interruption before POST may therefore leave a plan
unknown even when no remote run exists; this deliberately favors avoiding duplicates.

## Manual reconciliation

After at least 70 seconds, inspect Bitbucket and identify the pipeline UUID. Verify
its parameters against the preview yourself. Then call
`POST /api/plans/{id}/reconcile` with:

```json
{"confirmed":true,"verified_parameters":true,"pipeline_uuid":"12345678-1234-1234-1234-123456789abc"}
```

This endpoint only reads Bitbucket. It checks UUID, repository endpoint, commit,
branch and selector, then links the pipeline to the unknown plan. Parameters are
an explicit operator attestation, not an automatically verified correlation.
A target mismatch leaves the record unchanged. This operation never retries trigger.
Automatic discovery/reconciliation, report collection, and frontend Run controls
remain subsequent work. Remote polling and cancellation are described in
[monitoring.md](monitoring.md).

Only administrator-curated, non-secret parameter values belong in the registry;
these values and project snapshots are stored and returned by the local API.
Repository-level secrets remain managed by Bitbucket. Imported pipeline definitions
are resolved by Bitbucket; pinning the importing commit does not pin imported branch
heads. This local API is unauthenticated: bind to loopback and retain its origin guard.

Reference: https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pipelines/
