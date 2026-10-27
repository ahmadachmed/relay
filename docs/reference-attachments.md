# Optional conversation reference documents

Reference documents are optional. Existing report analysis and approved project or
source knowledge still work without them. In planning or a saved-run conversation,
upload a TXT, Markdown, text-based PDF or DOCX document. Select it under “Reference
document” and check the explicit sharing checkbox for the next AI message. You can
also approve a report for the same message; both files must belong to the same
conversation. Consent resets after each request. A local model gateway may forward
the excerpts upstream; redaction is best effort.

Relay ranks document text using words from the message and shares at most three
sections. TXT/Markdown use line locations, DOCX uses paragraph locations and PDF
uses page locations. Each section is redacted before being limited to a 1,500-character window,
centered near the earliest matching query term when available. Offsets refer to
the redacted text. The response shows the original attachment SHA-256, shared sections,
truncation and how many text sections were omitted. Reference citations must point to
one of those sections; citation membership does not prove that a claim is correct.
Documents are user-provided reference data, not proof of what was deployed. The model
is instructed to treat their contents as data rather than executable instructions.

Extraction runs in a separate process with an eight-second deadline, five CPU seconds
and a 512 MiB address-space limit. Inputs are limited to 5 MB, 100 PDF pages, 2,000
sections and 200,000 extracted characters. DOCX document XML is limited to 2 MB and
DTD/entities are disallowed; external relationships are not followed. The original
attachment storage limits and retention continue to apply. Invalid or encrypted files,
unsupported content and exhausted limits return explicit errors. Scanned PDFs with no
extractable text require OCR, which is not available. Mixed PDFs disclose pages without
extractable text. Images in all formats are not interpreted.

This is a first, bounded retrieval slice, not full-document analysis: ranking can miss
relevant text, section continuation and document-wide model-directed search are not
implemented, and only the shared snippets support citations. Reference attachment
sharing uses one-request consent; task-scoped approval, deny/resume, expiry, and
multi-file references remain follow-ups. Reference attachments can accompany automatic
report search, but that search still cannot be combined with registered original-source
retrieval in the same request. No source establishes deployed behavior on its own.

For local testing, install dependencies with `python -m pip install -e '.[dev]'`, build
the frontend, start the backend on loopback, then attach synthetic report and document
fixtures. Review the displayed sharing and coverage before asking the model. Do not
publish private test reports or TRDs in the repository or public issue.

Automatic report discovery has a 110,000-byte cumulative serialized model-input
budget across rounds, with a 64,000-byte per-call limit. This accommodates the
additional citation schema; budgets measure serialized input rather than tokens.
