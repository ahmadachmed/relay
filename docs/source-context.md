# Commit-pinned source preview (#29, phase B foundation)

This backend-only read-only capability previews an explicitly approved automation source file. It does not yet retrieve relevant code automatically, feed source to AI, or expose source controls in the UI. Knowledge directory support remains separate.

## Configuration

Set `RELAY_SOURCE_ENABLED=1` and the existing `RELAY_BITBUCKET_EMAIL` / `RELAY_BITBUCKET_API_TOKEN` credentials in the backend. Add file and/or directory permissions to the project's registry entry:

```json
"source_files": ["shared/assertions.py"],
"source_directories": ["tests/", "keywords/", "pages/"]
```

Restart the backend after configuration changes. Existing runs can use the current file allowlist only if the current enabled project still refers to the same Bitbucket repository as the saved run plan. A remapped/disabled project is denied. The saved run commit is mandatory; there is never a fallback to main/HEAD.

Example local request (replace RUN_ID):

```bash
curl -sS -X POST "http://127.0.0.1:8000/api/remote-runs/RUN_ID/source-preview" \
  -H 'Content-Type: application/json' \
  -d '{"path":"tests/test_api.py","confirmed":true}'
```

The caller cannot supply a repository, commit, URL or credentials. The response contains run/project/repository identity, commit, relative path, original-byte SHA256, a source ID and redacted text. Original hashes identify source bytes before redaction; redaction is best-effort, and the preview should be inspected before any sharing. The preview is returned only locally; it is not stored in a source cache or automatically sent to a model. Never publish company source in public issues or fixtures.

## Read policy

A file is approved when it matches source_files exactly or lies underneath source_directories. Directory entries include subdirectories, normalize an optional trailing slash, and must not name root, hidden/parent paths, globs or secret-bearing directories. Folder boundaries are enforced: tests/ does not approve tests-other/. Defaults are empty, so source_files remains backward compatible. Directory permission is not directory listing or a bulk read: each preview still names one file and performs the same metadata checks and per-file byte limit. No folder is automatically sent to AI.

Only approved relative file paths with ordinary ASCII letters/digits/underscore/hyphen/dot/slash are accepted. Empty/dot/parent/hidden path components and credential/secret/password/private-key names are denied. Supported text extensions: .py, .robot, .resource, .yaml, .yml, .json, .toml, .md. This is a restrictive first policy, not a guarantee that an allowed file contains no secrets.

The adapter requests metadata from the saved repository and full commit, verifies file type/path/commit/size/attributes, then reads that same pinned file. Only regular text file metadata (optional executable attribute) is accepted; executable code is still never executed. Link, subrepository, binary, directory, oversized and malformed responses are rejected. Maximum raw file size is 24000 bytes, metadata 6000 bytes, one active request per process, 20-second total deadline, no retries, redirects or environment proxies. UTF-8 and no NUL/private-key markers are required. Credential-bearing remote links are never followed.

The implementation uses direct GETs, not Git checkout, installs or hooks. It creates no source cache, so no disk retention/cleanup is needed here. Authentication/read failures are explicit; returned upstream error bodies and credentials are suppressed. Source IDs are not report artifact citations. Future retrieval/chunk citation integration must separately validate context actually shared with the model.

Provider reference: https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/

Remaining #29: relevant-file selection, bounded helper traversal, source-category AI consent, model context integration, code citations/provenance, UI controls and evaluations. This delivery does not close #29.
