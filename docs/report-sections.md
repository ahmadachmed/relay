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
