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
