# Private project knowledge (issue #29, first delivery)

Analysis uses the selected run's saved plan identity (project, framework, repository, branch and commit). The execution dropdown is a prospective plan selection and is not the analyzed run identity. Repository source retrieval is not implemented in this delivery.

Set `RELAY_KNOWLEDGE_ROOT` to a private directory outside your public checkout. Under it, create `<project-id>/manifest.json` and the explicitly listed document files. Symlinks anywhere along the configured path are rejected; use the real absolute directory path. The root is backend configuration, never a model/user-supplied path.

Example manifest (synthetic; use your actual review date and document metadata):

```json
[
  {
    "id": "overview",
    "path": "overview.md",
    "project_id": "api",
    "environments": ["uat"],
    "type": "overview",
    "revision": "1",
    "sha256": "<SHA256 of overview.md>",
    "owner": "QA team",
    "source": "QA-maintained project guide",
    "verification": "verified",
    "reviewed_at": "2026-10-26"
  }
]
```

The only allowed document filenames are `overview.md`, `business-rules.md`, `troubleshooting.md`, and `api-contract.yaml`. Content is UTF-8 text, not executable YAML, templates or code. Files must be regular, at most 8000 bytes each; at most four manifest entries (12000 manifest bytes) and 20000 document bytes per request are allowed. Existing AI request budgets still apply after redaction. Embedded secrets are excluded/redacted best-effort, not guaranteed absent: inspect what you approve for sharing.

Set sha256 to the actual file hash after reviewing its content (for example `shasum -a 256 overview.md` on macOS). Changed content is excluded until its reviewed manifest hash is updated.

Documents require matching project and the single environment parameter from the saved plan (parameter kind `environment`). Missing/ambiguous environments are not guessed. A verified entry needs nonempty type/revision/owner/source and a review date within the last 180 days, never in the future. Unknown/unverified/stale documents are omitted with diagnostics. Operator verification is not independent proof that a business rule is correct. Do not approve AI hypotheses as facts without review.

Enable **Settings → AI context sharing → Include approved project knowledge for the selected run**. This category defaults off and its preference persists in this browser. General AI sharing must also be enabled. The backend reads current files on each AI request; no model can write to this directory. Included text is redacted before the provider call and treated as untrusted instruction data. Through 9router, content may be forwarded to upstream models. No private file is uploaded into this public repository.

Observed report summary shows saved run identity and **Project knowledge used**, including document IDs, revisions, hashes, review dates and omission diagnostics. The metadata is the version snapshot used by that response; raw document text is not returned by that panel. Changing files requires a new AI request for new results. This is a per-response manifest, not durable analysis auditing. Document source IDs cannot be used as report artifact citations; separate knowledge/code citation support follows in later deliveries.

Missing configuration, invalid manifests, blocked paths and failed reads produce diagnostics, not invented context or a fallback to another project. The root path and read exceptions are not exposed to the provider/UI.

Remaining #29 work: immutable repository file access, relevant-code retrieval and source citations, durable source manifests/auditing, stale analysis handling across external document edits, quality evaluation, and reviewed knowledge maintenance. This delivery does not close #29 or #7.
