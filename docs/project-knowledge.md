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

## Scoped knowledge (schema version 2)

Automation and service knowledge have independent roots:
`automation/<project_id>/` and `sut/<sut_id>/` beneath `RELAY_KNOWLEDGE_ROOT`.
A pipeline may declare up to 16 unique `sut_ids`. A plan may select one or
explicitly leave it unset; the selected value is saved in its plan snapshot.
This identity does not establish which service version was deployed.

New manifests use the following wrapper around the existing document metadata:

```json
{
  "schema_version": 2,
  "category": "sut",
  "scope_id": "service-a",
  "documents": []
}
```

Each document retains its `path`, `environments`, `verification`, `owner`,
`source`, `type`, `revision`, `reviewed_at` and approved `sha256`. Do not supply
`id`: it is derived from the allowlisted filename stem, e.g. `overview.md`
becomes `overview`. Content edits preserve the document ID but change its
hash and opaque source ID. Category and scope isolate source identities.
Existing file, review and size restrictions remain in force.

Legacy `<project_id>/manifest.json` lists remain readable as automation only,
with their original explicit document/source IDs and a deprecation diagnostic.
To migrate, move the bundle into `automation/<project_id>/`, wrap the manifest
in the version-2 object and remove document `id` and `project_id` fields.
When the new scope exists it takes precedence, even if invalid: old and new
bundles are never merged and invalid new bundles never fall back silently.
No removal date for legacy support is set yet.

The category loader and plan contract are backend foundations. Independent
AI sharing, SUT selection UI and AI-authored knowledge proposals are subsequent
deliveries under issue #32. Existing AI sharing currently loads automation only.

### UI sharing and saved SUT identity

Select an optional **System under test** when the pipeline declares `sut_ids`.
Preview shows the choice; changing project/pipeline resets it. Run history and
selected-run information use the saved identity rather than the draft selection.
Old runs without an identity display `None` and cannot infer one from tags.

Settings contains separate automation and SUT knowledge sharing controls, both
off by default. Existing project-knowledge preference remains the automation
preference. Changing either clears displayed AI analyses; settings persist
independently. General AI sharing consent is still required. Analysis displays
category/scope, revision/hash, review date and omissions for the sources shared.
Document text is sent only to the configured model, not returned in provenance.
