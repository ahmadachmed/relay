# Source-aware AI context (#37)

Original approved sources can now be retrieved in planning without a saved run,
or used alongside a saved run's report evidence. They do not have to fit the
four curated knowledge filenames or the curated 8,000-byte file limit. Curated
bundles, explicit knowledge review/activation, rollback and report citations
remain separate. No source operation can execute/cancel a pipeline, edit an
allowlist, approve a knowledge proposal, or write active knowledge.

This is a local, single-operator feature, behind `RELAY_SOURCE_CONTEXT_ENABLED=1`.
Do not expose the unauthenticated local API as a hosted multi-user service.
Source access permission, permission to send content to a model, verified claims,
and evidence of deployed behavior are four distinct things.

## Configure

1. Copy `sources.example.json` to a private `sources.local.json` (gitignored).
   Set each local `location` to an absolute private directory outside the checkout.
   Protect that directory, configuration and `RELAY_DATA` with OS permissions.
   Root permission/privacy is an operator configuration responsibility.
2. Register original filenames under `files`, or local documentation folders under
   `directories`. The `scope_id` is a project ID for automation, or a declared SUT
   ID for SUT sources. Add the SUT ID to the relevant pipeline's `sut_ids` in the
   project registry; the sample project declares `service-a`.
3. Set backend environment:

   ```bash
   export RELAY_SOURCE_CONTEXT_ENABLED=1
   export RELAY_SOURCES=/absolute/private/sources.local.json
   export RELAY_AI_ENABLED=1
   # Keep your existing AI model/provider configuration.
   python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
   ```

   For Bitbucket, keep the existing backend-only `RELAY_BITBUCKET_EMAIL` and
   `RELAY_BITBUCKET_API_TOKEN`. An automation source repository must match the
   current enabled project and the saved run repository. For SUT repository
   sources, add `revision` containing a full 40-character commit. To offer several
   approved SUT revisions in the UI, register separate source IDs with those
   revisions; operators select the desired source/version, never an arbitrary URL.
4. Build the UI (`cd frontend && npm ci && npm run build`) and open `/ui/`.
   Backend config path changes need a restart; source entries are reloaded for
   each search/read/model round. Reload the UI to refresh the available catalog.

`RELAY_SOURCE_ENABLED` controls the older single-file source-preview API; it does
not enable original-source AI sharing. `RELAY_KNOWLEDGE_ROOT` controls curated
bundles; it does not register original sources. Disabling this feature leaves
ordinary assistant requests and existing knowledge bundles usable.

## Use and local acceptance

1. Choose an enabled project/pipeline, an explicit environment and a declared SUT.
   Do not preview or execute a pipeline; no saved run is needed.
2. Open **Original sources · planning and analysis**, select the contract and/or
   rules source, and enable **Share selected original sources with AI**. This
   consent starts off and is not inherited from curated knowledge settings.
3. Ask `Jelaskan GET /voucher/{id}`, or use a tag/operationId such as `getVoucher`.
   Relay retrieves the operation, required schemas and relevant documentation.
4. Inspect **Original sources used**: relative path, section/line selector,
   original SHA-256, separately hashed redacted shared text, parser version and
   repository commit when applicable. Model source claims cite request-scoped
   section handles. These are not report artifact IDs or proof of semantic truth.
5. Change a source file and click **Check source freshness** on the old response.
   It reports changed/unavailable/configuration-changed without rewriting the old
   hash. Ask again to create a new snapshot and response. Referenced sidecar schema
   files are rechecked too. Remote versions are pinned, not probed for newer HEAD.
6. Ask for an unknown endpoint, remove a dependency, or lower `context_bytes`:
   missing/unsupported/over-budget context must be visible. No partial-looking
   contract is sent when required dependency extraction fails.
7. Switch run/project/SUT/environment or sharing controls: a late response must
   not appear in the new selection. Run analysis uses its saved project/SUT and
   explicit environment, irrespective of the prospective execution dropdowns.

In planning, optional curated automation/SUT bundles can also be shared without a
saved run, using their independent settings. Operator analysis inputs and
knowledge proposals derived from failure evidence still require a saved run.

## Retrieval and supported subset

- Local reads traverse directories using no-follow file descriptors and accept
  bounded regular UTF-8 files. Hidden/traversal and sensitive-name components,
  symlinks, unsupported suffixes, NUL and private-key markers are rejected. Allowed
  suffixes: `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.py`, `.robot`, `.resource`,
  `.toml`. A safe filename does not guarantee secret-free content.
- Discovery is bounded to 4 nested levels, 256 examined entries and 128 registered
  or discovered files per source. Request read limits below still apply. Exclusions
  win. Bounded directory scanning can omit files and explicitly discloses that.
  Remote discovery deliberately uses explicitly registered files only.
- OpenAPI 3.0/3.1 and Swagger 2.0 operations are ranked by method/path,
  operationId, tags, summary/description and lexical terms. An explicit unknown
  method/path does not fall back to a different operation. Root paths, template
  parameters and prose/Markdown delimiters are supported.
- The parser preserves operation and path parameters, request/response objects,
  applicable security, effective media/transport settings, schema composition and
  referenced dependencies. JSON Pointer fragments are decoded and validated.
- Cross-file refs can only use already snapshotted, explicitly authorized files
  in the same selected source. Repository dependencies use the same full commit.
  A ref never fetches a new path or network URL. Relative refs can traverse to
  another registered path within that source, but cannot escape its approved
  snapshot. Cycles are bounded; unregistered/budget-omitted refs fail explicitly.
  Derived internal ref names are accompanied by original dependency hashes.
- Referenced path items, URI anchors, JSON Schema `$id`/dynamic/recursive-reference
  semantics and arbitrary external network refs are outside the supported subset.
  Required unsupported constructs produce diagnostics, not a complete contract.
- Text/Markdown/automation use 40-line sections, stable line selectors and original
  file hashes. Import/resource hints support static Python `import module` /
  `from module import ...`, and Robot `Resource` paths. Hints point only to indexed,
  authorized sections in the same source. Dynamic imports, variable expansion and
  code execution are not supported. Models may request those handles or search
  for a helper with a narrower query.
- Run automation reads require the saved full commit and current enabled matching
  repository. There is no HEAD fallback. Local automation reference files cannot
  prove the saved commit and are omitted in run analysis. SUT revisions remain
  separately labelled references; deployed version is unknown.
- No embeddings, vector database, persistent derived index or raw source archive
  is created. Each request re-reads sources; only request-memory snapshots are used.
  Snapshot handles expire with the request and cannot be reused in later calls.

## Budgets and model loop

The optional `limits` object can adjust these bounded settings; values above
the documented maxima are rejected. Limits are captured for each request.
For guided tasks they are shown on the approval card and bound to the grant.

| Setting | Default / maximum |
|---|---:|
| `files` | 16 attempts, including missing/invalid files |
| `read_bytes` | 2,000,000 source/remote-response bytes |
| `sections` | 256 indexed sections |
| `candidates` | 8 per search |
| `context_bytes` | 20,000 serialized redacted section bytes |
| `model_rounds` | 3 default, 12 maximum |
| `model_input_bytes` | 100,000 default, 600,000 maximum (minimum 48,000) |
| `wall_seconds` | 40 default, 300 maximum for retrieval + model rounds |

The existing context ceiling remains 48,000 bytes per call, with a 64,000-byte
serialized provider-payload ceiling including instructions, output schema and JSON
escaping. Cumulative model input also counts that full payload on every call. Model
output remains capped at 2,000 tokens per call. Source body reads can consume one
extra sentinel byte to detect overflow; the sentinel is accounted and no further
read is allowed after the aggregate ceiling. Remote metadata counts toward the
read budget. Missing/open-denied files consume zero content bytes, but still count
as attempts. YAML/JSON nesting and event limits apply before object construction;
reference extraction also has node and output-byte ceilings.

Structured-output adapters use typed `search_sources(query)` and
`read_source_section(handle)` requests. Operations are backend-validated and
recheck current authorization even for in-request cached snapshots. Each model
call sends a new bounded context; sections, discovery metadata, history and
repeated input count toward budgets. Exhaustion returns a partial answer with
coverage diagnostics. A `json_object` gateway uses visibly disclosed deterministic
one-pass retrieval and cannot silently acquire follow-up tools. There are no
native shell/network/execution tools exposed to the model. Monetary cost depends
on the configured model; call count, output-token and cumulative input-byte caps
bound exposure without claiming a price estimate.

Base chat identity/instructions/report evidence are assembled first. Optional
source text never displaces them. If source text/metadata cannot fit, it is omitted
with local response diagnostics and report/chat-only analysis can continue.

## Provenance, retention and privacy

`GET /api/source-registry` returns safe metadata, never local roots or credentials.
`GET /api/source-manifests/{response_id}` rechecks current source access and local
fingerprints. Every accepted source citation must name a section included in that
exact response's model-call sequence, after redaction and final context selection.
Discovery metadata alone cannot support a source citation. This validates
provenance, not whether a model claim follows logically from its source.

A private `source-manifests.sqlite3` (mode 0600) in `RELAY_DATA` holds per-response
identity, original/dependency hashes, parser version, safe section metadata,
redacted-shared-content hash and omission diagnostics. It stores no raw text.
Records expire after 30 days; new responses prune old records and retain at most
200. Metadata-only provenance does not promise full answer replay. Delete this
file while the backend is stopped to erase all retained source manifests.

Redaction and untrusted-data instructions are mitigations, not guarantees against
secrets or prompt injection. Review sources before enabling sharing. Model output
remains escaped text in the UI and cannot authorize changes. AI knowledge drafts
still require the existing exact-content operator review and activation flow.

## Automated acceptance and troubleshooting

```bash
python -m pytest -q
cd frontend
npm run typecheck
npm run build
npm run test:e2e
```

Synthetic tests cover planning without runs, source refresh/provenance, endpoint
and schema extraction, same-source refs/cycles, Unicode/budget omission, independent
consent, scoped/expired/fabricated handles, disabled/remapped sources, a bounded
follow-up read, pinned Bitbucket metadata and repository identity, freshness,
model limits, and late browser responses. Backend tests use a deterministic fake
model and `httpx.MockTransport`; they do not send private data to live services.
Passing these tests does not establish real-model reasoning quality. Live private
source evaluation is a separate operator activity.

If the source controls are absent, check the feature flag, `RELAY_SOURCES`, restart
and UI catalog reload. If no sources apply, check declared SUT, project ID and the
explicit environment. If a contract is missing, inspect diagnostics for file,
parse/ref/size/index limits, then narrow the selection or raise a lowered limit
within its ceiling. A changed source needs a new request, not replay of an old
manifest. A missing immutable run commit never permits falling back to HEAD.
