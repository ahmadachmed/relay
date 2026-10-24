# Opt-in QA assistant

Issue #7 adds an optional OpenAI Responses adapter above the existing execution boundary. Structured commands work without AI. No model API call is made unless AI is enabled on the backend and the operator explicitly checks the sharing control in the UI.

## Configure locally

Set these in the shell that starts the Python backend. Do not put credentials in project JSON, browser storage, committed files, or chat:

```bash
export RELAY_AI_ENABLED=1
export OPENAI_API_KEY='your-provider-key'
export RELAY_AI_MODEL='your-available-model-with-structured-output-support'
python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
```

Choose a model available to your account that supports strict Structured Outputs in the Responses API. No model is selected implicitly. Rebuild the frontend using the existing [workspace instructions](workspace-ui.md). Refresh the page after changing backend configuration. This is a local single-user server; deployment authentication remains out of scope.

## Workflow

Check **Send chat…to AI** to enable natural-language messages. The disclosure covers the message, up to eight recent conversation turns, enabled project IDs/frameworks/allowed branches/pipeline parameter catalogs, current context, and the selected run's report summary. Repository URLs, credentials, raw report bytes, console logs, and files are not sent. Project IDs and parameter values may themselves be sensitive: review your configuration before opting in.

By default, run analysis sends statuses, aggregate counts, artifact IDs/availability/parse status/manual association, and whether attempts are reported. Failure names and messages are excluded. The separate **Include failure names/messages** checkbox includes at most twenty failures, with redaction before truncation. Redaction masks known environment secrets, secret-bearing fields/assignments, bearer credentials, private key blocks, URLs, emails, and phone-like numbers. This is best-effort minimization, not a guarantee that arbitrary business information or all PII is removed. Inspect sensitive reports before opting in. Disclosure is per page session and resets on reload.

AI can clarify a request, propose one configured selection, analyze the selected run, or suggest reviewing a cancellation. A proposal is validated against the registry. Sending a natural-language follow-up invalidates any old preview. **Use proposed context** updates the selection; the user must then preview the pinned commit and click **Confirm & run**. Multi-project or ambiguous requests should be clarified. Follow-ups include the last validated, unapplied proposal separately from the current UI selection. It remains in page memory across clarification/error responses and resets on context changes, run changes, new checks, or reload. The backend validates it against the current registry before sending it to the model. Follow-up context changes require another preview. The model has no execution, shell, file, arbitrary URL, cancellation or publication capability. A cancellation suggestion only exposes the existing confirmation flow; it never sends a stop request by itself.

Observed pipeline/report facts are rendered separately from model guidance. Hypotheses are explicitly unverified and must reference available parsed artifacts for that selected run; missing or expired evidence cannot support citations. Attempt counts remain unknown when the source does not report them. Bug reports remain reviewable drafts; nothing is published automatically. Citation validation checks artifact ownership/availability, not whether a model's interpretation is semantically correct.

Conversation continuity uses the existing local browser history. Only the current page's latest AI suggestion card is held in memory; raw report text is not persisted by this feature. Treat model prose as untrusted guidance and verify it against the report links.

## Bounds and failure behavior

- One model request at a time per server process; competing requests receive HTTP 429.
- Input is capped at 48,000 UTF-8 bytes after redaction; each message is at most 4,000 characters and history at most eight turns. Oversized catalogs are rejected rather than silently truncated.
- Output is capped at 2,000 tokens; the total request has a 40-second deadline and a 128 KB response limit. Input bytes bound the payload, not an exact tokenizer estimate. Configure account-level spend limits separately.
- Requests use the fixed HTTPS OpenAI Responses endpoint with `store: false`, no redirect following, and no automatic retry. This setting does not establish a zero-data-retention agreement; follow your provider account's applicable data policy.
- Incomplete/refused/malformed model responses, invalid selections and unsupported citations are rejected. Provider error bodies are not returned to users or logged by this implementation. Disabling AI does not disable manual preview/run/status/evidence controls.

## Verification and remaining acceptance

Run `python -m pytest -q`, build the frontend, then `npm run test:e2e --prefix frontend`. Unit and HTTP tests use synthetic providers and an intercepted Responses transport. They cover the three registry stacks, follow-up history, ambiguous/multi-project clarification contract, redaction before truncation, evidence ownership/missing evidence, unknown attempts, invalid actions, explicit consent, input budget, refusal/incomplete output, and absence of mutation capabilities. Browser fixtures cover sharing opt-in, proposal application/preview invalidation, cancellation confirmation, and provider-error recovery.

No paid model calls or private pipelines are used by these tests. A live model evaluation with operator-approved configuration/data is still required for language quality, ambiguity handling and prompt-injection resistance. This is an initial delivery of #7, not a claim that all pilot or redaction acceptance is complete. Test-name discovery, multi-step autonomous tool use, streaming, pricing-based daily budgets, and deployment auth are not implemented.

Protocol reference: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). The adapter uses `text.format` with a strict JSON schema and validates the result locally again.
