# Local Robot attachment inspection

Part of #45 / #47 M1. After uploading an attachment, GET
`/api/conversations/{conversation_id}/attachments/{attachment_id}/robot-report`.
Default `offset=0&limit=20`; maximum limit is 50. The default view lists FAIL tests.
Use returned test `id` as `test_id` to page through that test's keyword, message,
argument and status records. IDs are ordinal within the immutable file hash;
retain attachment ID and SHA-256 alongside any section reference.

This endpoint is local inspection only. It does not send file content to AI,
associate uploads with a pipeline, or provide a diagnosis. Existing single-user
loopback deployment restrictions apply. Contents may contain sensitive data.

Parsing rejects malformed XML, DTD/entities, non-Robot roots, more than 100,000
nodes, depth over 64 and reports over 10 MB. Each request reparses the bounded
original; persistent indexing is not implemented. Text records are capped at
2,000 characters with explicit truncation. Traversal records are end-event ordered;
this is not a reconstructed keyword tree. Suite setup/teardown outside tests is
not returned. Reports without tests return zero rather than a passing verdict.
Deletion/expiry removes access. No derived content is persisted.

Remaining #45 work includes suite-level evidence, structured hierarchy/search,
shared saved-run retrieval, document extraction, AI tools/citations, task approval
continuation and frontend inspection. This slice does not complete #45 or M1.

## Share one test with AI (initial slice)

In the conversation, select an uploaded XML report, choose a failed test, optionally
set the starting section, and explicitly check the sharing checkbox. The next AI
message sends up to 20 redacted section records and test metadata to the configured
model provider (which can be upstream of a local gateway). Consent is per request;
a retry requires selecting and approving sharing again. A scope-bound attachment ID,
original SHA-256, test ID, and offset are checked on the backend before each model
round. Deletion, expiry, wrong conversation and changed hashes reject the request.
The attachment remains a user-provided reference; it is not automatically bound to
any pipeline or evidence slot. Full files are not sent. Redaction is best effort.

The response displays the redacted records provided to the model and coverage so
users can inspect the basis of the answer. Report citations are accepted only for section IDs shared with the model in that
request; membership does not validate the meaning of a claim. Existing saved-run classification and its
citations still require saved-run evidence; report attachments alone do not enable
that flow. Report text and names are redacted before truncation. Records may be omitted to fit the serialized AI input budget; the returned next offset identifies the first omitted section. Each record is returned in chunks of up to 2,000 redacted Unicode characters.
For a long record, choose “Use next part” and approve sharing again. The continuation
is bound to the same attachment, test, section and SHA-256 of the complete redacted
text; changed redaction invalidates the continuation. Character offsets count Unicode
code points in the redacted text, not bytes or positions in the original. Suite-level setup/teardown and
unread records remain absent. Model-chosen retrieval, task-scoped resumable approval,
semantic verification of citations, automatic full-report retrieval, and other attachment
formats are future #45 work. This feature does not complete M1.

## Report search and automatic discovery (bounded)

Select an uploaded Robot XML report and use **Search locally** for literal,
case-insensitive matching across test names, keyword names, test-section text,
suite setup/teardown and suite status messages.
Results are paginated and contain the test/section IDs, suite path and a bounded
excerpt. A search result may be a passing test; selecting it does not send anything
to AI. A separate checkbox approves sharing selected test sections.

For AI discovery, check “Let AI search this report” for the next message. This
explicitly allows searching names, keywords and logs of passing as well as failing
tests. Existing API clients that only send `share: true` retain failure discovery;
`search_all: true` is required for the broader search operation. Search results
are discovery, not citable evidence: the model must read a discovered test section
before citing it. Scope, attachment hash and redacted search snapshots are checked
again throughout the request. The UI trace reports counts and offsets, not raw
queries. The same search and suite parsers are available for saved-run Robot evidence. A
saved run may share its report with AI after explicit consent; run identity, artifact
hash and retention are rechecked, and manual-upload association remains operator-
asserted rather than provider-verified.

Automatic discovery may be combined with separately approved original-source
sharing; source configuration, identities, model-round and input limits still
apply. A source-enabled request uses at most the configured source model rounds
(default three), so a complex investigation may require a narrower request.
Manual report selection and automatic discovery remain mutually exclusive.
Only the latest test detail page is retained for report citation. Search does not
establish report completeness, deployed behavior, flaky status or root cause.
Redaction is best effort and uploaded reports are not verified pipeline evidence.
With explicit `include_suites: true` consent, the model can list suites and read
setup/teardown sections for discovered suite IDs. Suite citations are validated
separately from test citations. Suite status may reflect a parent failure and must
not be treated as independent proof of every child failure. Existing API requests
without this consent cannot send suite content. For long test sections the model can request the offered next text offset within
the same request; Relay rechecks the redacted text hash before sharing. Only the
latest detail or continuation is citable. Task-scoped approval, resumable tasks
and broader coverage remain follow-up work under #45.

## Compare two uploaded reports

Select an earlier and later XML attachment in the same conversation. Local
comparison pairs only unique, nonempty suite/test paths; ambiguous duplicates and
unmatched tests are not guessed. Totals cover all unique matched pairs, while
individual results are paginated. The local view is not shared with AI. A separate
one-request checkbox shares the selected page and aggregate counts; comparison
citations are restricted to shared pair IDs. Hashes, deletion and redaction are
checked before and after model calls.

These files have unverified pipeline, revision, environment and attempt associations.
Matching names do not prove comparable runs. Fail-to-pass is an observed change,
not proof of flakiness or root cause. Saved-run history comparison, automatically
verified comparability and historical repeated-run analysis remain follow-ups.

## Guided investigation task approval

In guided mode, the message and eligible attachment, selected-run, related-run
and registered-source metadata can be sent to the configured model for planning.
File contents are not sent during planning. The model may propose report, reference,
comparison and source capabilities; the server validates every resource and the
operator reviews the scopes, hashes, purposes, provider and expiration before approval.
The approval is bound to the task, conversation, request, resource versions, run
identity and model recipient. A local model gateway may forward content upstream.

For registered sources, the first approval permits reading the chosen source locally
or from its configured provider. Relay then shows the prepared file hashes and requires
a second approval before source content is shared with AI. Changed files, source
configuration, provider, resource hash or redaction invalidate the task rather than
silently expanding permission. Denial or revocation prevents subsequent sharing and
results are discarded when access is revoked during model execution.

Pending tasks can be retrieved after refresh. Approval is claimed atomically and
repeated approval does not run the task twice. If an in-flight task is interrupted,
its lease expires and the operator must create and approve a new task; it is never
automatically replayed. Approval expires after 15 minutes. Task messages, decisions
and redacted results are kept in the local SQLite database (restricted to owner access)
for up to seven days after expiration, with lazy cleanup. This is a local single-user
workflow, not multi-tenant authorization.

Original sources must be registered and eligible for the selected project, SUT and
explicit environment. Saved-run report associations are operator-asserted. The model
sees only bounded candidate metadata during planning and cannot access arbitrary
paths, execute pipelines, change files or grant itself permissions.
