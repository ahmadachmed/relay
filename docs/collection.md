# Bitbucket pipeline Artifacts workflow

This corrects PR #15's repository Downloads integration. Reports in the pipeline
Artifacts tab are not repository Downloads. Relay no longer requests /downloads,
requires download_name, follows storage redirects, or needs
RELAY_EVIDENCE_DOWNLOAD_HOSTS. Remove those optional settings before restarting.
Existing plans and evidence bytes are retained; old collection diagnostics are no
longer returned as current status. There is no private pipeline change required.

POST /api/remote-runs/{run_id}/evidence/collect and
GET /api/remote-runs/{run_id}/evidence/collection return a manual handoff containing
the saved pipeline URL, configured report paths, local evidence availability, and
`automatic_download_supported: false`. They do not inspect remote artifact existence
or claim to have downloaded a file. Open the pipeline URL, choose Artifacts, download
the report and upload it through the existing evidence API:

```bash
curl -sS -X POST \
  "http://127.0.0.1:8000/api/remote-runs/$RUN_ID/evidence?report_path=output.xml" \
  -H 'Content-Type: application/xml' --data-binary @output.xml
```

Manual source association remains operator_asserted. Extract ZIP files locally;
Relay does not unpack uploaded archives. Local evidence availability is independent
of parse success: inspect the evidence summary's test_status and parse_status.

The scheduled seven-day evidence cleanup from PR #15 is retained, running on startup
and every 60 seconds while the backend runs. Access-time expiry also remains.

Automatic download from pipeline Artifacts is not implemented: Atlassian's public
API feature request BCLOUD-19330 remains unresolved/Gathering Interest. A future
transport needs a verified supported mechanism; repository Downloads is not silently
substituted and browser session credentials are not requested by this backend.

Reference: https://jira.atlassian.com/browse/BCLOUD-19330
