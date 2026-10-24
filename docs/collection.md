# Optional Bitbucket Downloads collection

This extends the manual evidence API. It does not use undocumented pipeline-artifact
endpoints. Bitbucket repository Downloads and the Artifacts tab are distinct. This
transport works only if the report is explicitly published to repository Downloads.
No private pipeline is edited, triggered or cancelled by configuring collection.

## Configuration

Add an optional mapping to a report in projects.local.json before creating a plan:

```json
{"format":"robot_xml","path":"output.xml","download_name":"output-{pipeline_uuid}.xml"}
```

The filename must contain `{pipeline_uuid}` (32 lowercase hex digits, no braces or
hyphens). `{commit}` is also supported. Only flat filenames up to 240 characters are
accepted. A shared `latest.xml` is rejected because it cannot identify a run.
Existing plans retain their original snapshot; mappings added later do not alter them.
Reports without a mapping keep their manual-upload workflow and never cause a remote
file request. Renaming a file alone does not cryptographically prove its origin;
remote imports are marked `pipeline_uuid_namespace`, not `operator_asserted`.
Repository writers remain trusted to publish the correct file for that UUID.

The private pipeline must publish the matching file after the tests, including on
failure if failure reports are needed. For the mapping above, remove `{`, `}`, and
`-` from BITBUCKET_PIPELINE_UUID and publish output-<uuidhex>.xml via the documented
repository Downloads upload API. Publishing requires write:repository:bitbucket in
the pipeline's own credential. Relay downloads require read:repository:bitbucket.
Keep XML generation/upload in the pipeline's appropriate after-script phase. Do not
replace or overwrite another run's file. Relay does not upload reports to Bitbucket.

Bitbucket's download API may redirect to signed object storage. Relay follows one
HTTPS redirect only when its EXACT hostname is listed by the backend operator:

```bash
export RELAY_EVIDENCE_DOWNLOAD_HOSTS="approved-storage-host.example"
```

Replace that example with the actual approved storage hostname. There is no default
wildcard or guessed S3 host. Unknown hosts fail with download_host_not_allowed;
credentials and signed URLs are never included in diagnostics. Storage GETs have
no Bitbucket Authorization header. Further redirects are rejected. Both direct
and redirected downloads are bounded to 10 MB, with a 60-second network timeout.

## Collect

Use a run whose remote status is terminal. Refresh the run if its saved observation
is still active. Then:

```bash
curl -sS --max-time 70 -X POST \
  "http://127.0.0.1:8000/api/remote-runs/$RUN_ID/evidence/collect"
curl -sS \
  "http://127.0.0.1:8000/api/remote-runs/$RUN_ID/evidence/collection"
curl -sS \
  "http://127.0.0.1:8000/api/remote-runs/$RUN_ID/evidence"
```

Collection is on demand, not a background download on every completed pipeline.
It only performs GETs remotely. At most 20 report mappings are handled per request,
with a 65-second overall limit and a 90-second persisted per-run lease. Already
imported slots are not downloaded again. A missing file remains retryable and
cannot produce a passing test result. One report failing does not erase other
imports. If the backend exits while collecting, another request can resume after
the lease expires; completed artifact slots remain immutable and deduplicated.

Collection metadata distinguishes imported/available/expired, missing, failed and
not_configured per report. The normalized evidence summary remains separate from
pipeline status. A downloaded XML that fails parsing remains original evidence,
with no invented passing results. Empty or unsupported reports never claim success.
No model receives report bytes. Redacted AI analysis remains future work.

## Scheduled retention

The API lifespan now sweeps expired evidence at startup and every 60 seconds while
the backend is running, even without evidence reads. Access-time expiry still
protects downloads and summaries. The seven-day TTL is not extended by collection
or duplicate uploads. A stopped backend cannot perform physical cleanup; access
checks and startup cleanup apply after restart.

## Delivery scope

This is a stacked follow-up to PR #14. Existing manual configurations remain valid.
Live validation is intentionally deferred. Test fixtures use synthetic responses;
no company pipeline/source files are contacted during development. Pipeline Artifacts
UI download, general S3/GCS connectors, logs/screenshots/Allure discovery and redacted
analysis are not claimed by this optional Downloads transport.

References:
- https://developer.atlassian.com/cloud/bitbucket/rest/api-group-downloads/
- https://support.atlassian.com/bitbucket-cloud/docs/deploy-build-artifacts-to-bitbucket-downloads/
