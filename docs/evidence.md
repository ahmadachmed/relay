# Report evidence (first delivery for issue #5)

This delivery adds explicit local report upload and parsing for known remote runs.
It does NOT automatically fetch Bitbucket pipeline artifacts. The repository Downloads
API is a separate publishing surface; do not assume that it contains pipeline artifacts.
The pipeline's actual report publication mechanism must be established before adding
an automatic fetcher. No private pipeline configuration is changed by this feature.

## Local validation

Install updated dependencies (`python -m pip install -e '.[dev]'`) and restart Relay
with the same data directory. Download the actual output.xml for the intended run
from Bitbucket yourself. Upload its bytes for that run's configured report path:

```bash
curl -sS -X POST \
  "http://127.0.0.1:8000/api/remote-runs/$RUN_ID/evidence?report_path=output.xml" \
  -H 'Content-Type: application/xml' --data-binary @output.xml
curl -sS "http://127.0.0.1:8000/api/remote-runs/$RUN_ID/evidence"
```

Upload is an operator assertion of run association, explicitly recorded as
`source=manual_upload`, `association=operator_asserted`. Relay cannot prove that a
manually selected file came from that pipeline. Only paths configured in the
persisted plan are accepted. JUnit reports use the configured `junit_xml` format.

The evidence summary is separate from the saved remote pipeline observation;
monitoring refreshes do not overwrite parsed results. A pipeline success is never
used to infer test success. Missing, invalid, expired or unsupported reports are
explicit. Each normalized test references its original artifact ID. Suite-level
Robot setup/teardown failures and JUnit errors are retained. Attempts remain null
when source metadata does not establish retry counts. Retry reconstruction and
full provider-specific JUnit extensions are not implemented in this delivery.

## Storage and limits

Raw bytes, SHA-256, operator provenance and normalized results are stored in SQLite.
Each configured report slot is immutable: identical uploads return the original ID;
different contents return 409. At most 10 MB per file; archives are not extracted.
DTD/entity-bearing XML is rejected with defusedxml, including alternate XML encodings.
Malformed/empty XML is retained as evidence with parse_status=failed and never passes.

GET /api/remote-runs/{run_id}/evidence/{artifact_id} checks both run and artifact IDs.
All bytes are downloaded as attachment/octet-stream with sandbox/nosniff headers;
HTML is never embedded or executed by the API. Unsupported configured report formats
remain available as downloads, without a fabricated parser result.

Evidence expires seven days after upload. On evidence summary/download access,
expired blobs and normalized test messages are cleared and the artifact is marked
expired. This is lazy expiry, not an idle-backend deletion guarantee. Encrypted
storage, hosted user authorization and scheduled retention sweeps remain deployment
work; retain the local API loopback restriction.

Raw report bytes and messages can contain sensitive data. Raw files stay local. Optional AI sharing sends a redacted summary and, when enabled in Settings, bounded failure names/messages; see docs/assistant.md.

`analysis_ready` means at least one test result references an available, parsed artifact. It is not an AI job status, evidence-completeness guarantee or permission to share. `evidence_complete` and `evidence_limitations` describe missing, expired, unsupported or failed-parse reports. Unknown attempts do not block bounded analysis of available results. No background analysis task is implied. A partial report set can support analysis while its missing evidence remains explicit.

Remaining issue #5 work: verified automatic provider artifact transport, console and
related screenshot/trace/Allure discovery, source-specific retry metadata, scheduled
retention and a redacted AI-analysis handoff. The issue stays open.

References:
- https://developer.atlassian.com/cloud/bitbucket/rest/api-group-downloads/
- https://support.atlassian.com/bitbucket-cloud/docs/use-bitbucket-artifacts-in-step/

## Allure test-result JSON (initial support)

Configure a report slot before creating a new run:

```json
{"format": "allure_json", "path": "allure-result.json"}
```

Upload one original `*-result.json` file to that slot (the configured path is the slot name, not a glob). Each original Allure result file represents one test execution, not the entire launch. Multiple known files require separate configured slots. Existing run plans retain their original report configuration; changing the registry affects new runs only.

Statuses passed/failed/broken/skipped/unknown map to pass/fail/error/skip/unknown. Name, message and trace are normalized; raw bytes stay available for download. An explicitly unfinished stage is unknown. Attempts remain unknown: flaky/history identifiers alone do not establish rerun counts. No attachment paths, URLs or scripts are opened. ZIP, arrays, container files, attachment bundles and launch discovery are not supported. A malformed result is recorded as parse failed.

Coverage is explicitly partial: even an uploaded passing result does not make the whole run passed. Allure slots keep evidence_complete false and disclose missing launch/container/attachment/retry coverage, while parsed failures can support bounded AI analysis under the existing sharing settings.

Format reference: https://allurereport.org/docs/how-it-works-test-result-file/
