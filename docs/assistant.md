# Opt-in QA assistant

Issue #7 adds an optional OpenAI Responses adapter above the existing execution boundary. Structured commands work without AI. No model API call is made unless AI is enabled on the backend and sharing is enabled in Settings → AI context sharing (on by default).

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

By default, run analysis sends statuses, aggregate counts, artifact IDs/availability/parse status/manual association, and whether attempts are reported. Failure names and messages are included by default. The **Include failure names/messages** setting can be disabled independently; when enabled it includes at most twenty failures, with redaction before truncation. Redaction masks known environment secrets, secret-bearing fields/assignments, bearer credentials, private key blocks, URLs, emails, and phone-like numbers. This is best-effort minimization, not a guarantee that arbitrary business information or all PII is removed. Inspect sensitive reports before using AI. Both sharing settings default to on and persist in browser local storage; an explicit off preference survives reload and run switching. Disabling general sharing prevents AI requests. These settings do not authorize pipeline actions.

AI can clarify a request, propose one configured selection, analyze the selected run, or suggest reviewing a cancellation. A proposal is validated against the registry. Sending a natural-language follow-up invalidates any old preview. **Use proposed context** updates the selection; the user must then preview the pinned commit and click **Confirm & run**. Multi-project or ambiguous requests should be clarified. Follow-ups include the last validated, unapplied proposal separately from the current UI selection. It remains in page memory across clarification/error responses and resets on context changes, run changes, new checks, or reload. The backend validates it against the current registry before sending it to the model. Follow-up context changes require another preview. The model has no execution, shell, file, arbitrary URL, cancellation or publication capability. A cancellation suggestion only exposes the existing confirmation flow; it never sends a stop request by itself.

Observed pipeline/report facts are rendered separately from model guidance. Hypotheses are explicitly unverified and must reference available parsed artifacts for that selected run; missing or expired evidence cannot support citations. Attempt counts remain unknown when the source does not report them. Bug reports remain reviewable drafts; nothing is published automatically. Citation validation checks artifact ownership/availability, not whether a model's interpretation is semantically correct.

Conversation continuity uses the existing local browser history. Only the current page's latest AI suggestion card is held in memory; raw report text is not persisted by this feature. Treat model prose as untrusted guidance and verify it against the report links.

## Bounds and failure behavior

- One model request at a time per server process; competing requests receive HTTP 429.
- Input is capped at 48,000 UTF-8 bytes after redaction; each message is at most 4,000 characters and history at most eight turns. Oversized catalogs are rejected rather than silently truncated.
- Output is capped at 2,000 tokens; the total request has a 40-second deadline and a 128 KB response limit. Input bytes bound the payload, not an exact tokenizer estimate. Configure account-level spend limits separately.
- The default adapter uses the HTTPS OpenAI Responses endpoint with `store: false`, no redirect following, and no automatic retry. The optional loopback Chat Completions gateway is described below. This setting does not establish a zero-data-retention agreement; follow your provider account's applicable data policy.
- Incomplete/refused/malformed model responses, invalid selections and unsupported citations are rejected. Provider error bodies are not returned to users or logged by this implementation. Disabling AI does not disable manual preview/run/status/evidence controls.

## Verification and remaining acceptance

Run `python -m pytest -q`, build the frontend, then `npm run test:e2e --prefix frontend`. Unit and HTTP tests use synthetic providers and an intercepted Responses transport. They cover the three registry stacks, follow-up history, ambiguous/multi-project clarification contract, redaction before truncation, evidence ownership/missing evidence, unknown attempts, invalid actions, explicit consent, input budget, refusal/incomplete output, and absence of mutation capabilities. Browser fixtures cover sharing settings, proposal application/preview invalidation, cancellation confirmation, and provider-error recovery.

No paid model calls or private pipelines are used by these tests. A live model evaluation with operator-approved configuration/data is still required for language quality, ambiguity handling and prompt-injection resistance. This is an initial delivery of #7, not a claim that all pilot or redaction acceptance is complete. Test-name discovery, multi-step autonomous tool use, streaming, pricing-based daily budgets, and deployment auth are not implemented.

Protocol reference: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). The adapter uses `text.format` with a strict JSON schema and validates the result locally again.

## Local 9router gateway

Run Relay and 9router on the same machine, then configure the backend shell:

```bash
export RELAY_AI_ENABLED=1
export RELAY_AI_BASE_URL='http://localhost:20128/v1'
export RELAY_AI_API='chat_completions'
export RELAY_AI_OUTPUT_FORMAT='json_object'
export RELAY_AI_MODEL='EXACT_MODEL_ID_FROM_YOUR_9ROUTER'
# If your 9router instance requires authentication:
export RELAY_AI_API_KEY='YOUR_9ROUTER_KEY'
python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
```

Select an actual model/route ID from your 9router dashboard or `GET http://localhost:20128/v1/models` (include the gateway Bearer key when required). The example model string above is a placeholder. `json_object` sends the answer schema in the system instructions and still validates every returned answer locally. Use `RELAY_AI_OUTPUT_FORMAT=json_schema` when the selected gateway route supports strict schemas. Relay does not retry automatically or switch formats on failure.

`RELAY_AI_BASE_URL` supports only the official `https://api.openai.com/v1` endpoint or loopback hosts (`localhost`, `127.0.0.1`, `::1`) with path `/v1`. HTTP is allowed for loopback. Credentials in URLs, query strings, fragments and remote custom hosts are rejected. Base URLs can have one trailing slash. `RELAY_AI_API` defaults to `responses`; the chat mode uses `/chat/completions`, non-streaming responses, a 2,000-token output limit and the same timeout/byte limits. Refusals, truncation and unexpected tool calls are rejected.

For loopback gateways, Relay sends only the dedicated `RELAY_AI_API_KEY` if configured; it never inherits `OPENAI_API_KEY`. Local HTTP connections ignore environment proxy settings and do not follow redirects. Gateway authentication policy is controlled by your 9router instance. The `/api/assistant` status check recognizes local model configuration without requiring an OpenAI key; it checks configuration, not live connectivity.

The sharing checkbox applies to the gateway and any upstream model service it routes to. Gateway logging, retention and forwarding policies are controlled by 9router; the Responses-specific `store:false` setting does not define gateway retention. The same explicit sharing controls, redaction, scoped citations and execution confirmation remain in place.

Restart the Relay backend after configuration and refresh the UI. Verify `/api/assistant` reports `enabled:true, configured:true`, review sharing settings, and send a harmless planning request first. Localhost refers to the machine/container running the Python backend. This connector was fixture-tested; connectivity and model compatibility on the operator's 9router instance still require a local smoke test.

Reference: [9router integration documentation](https://github.com/decolua/9router/blob/master/gitbook/content/en/integration/other-tools.md).

## Run conversations

Chat text is stored separately for each run in this browser, with a separate planning conversation. Before execution, the originating messages are saved under the plan/run ID so recovery can restore them even if the execute response is lost. Existing run messages are preserved. Unapplied proposals are scoped to their conversation during the page session, including history round trips. Switching history does not reuse another run's chat; legacy unscoped messages are ignored because their ownership cannot be established. New check resets planning without deleting run conversations. AI cards are retained per run during the current page session; after reload, chat text is restored but AI cards require a new request. Pending AI responses are discarded when the selection changes. Run progress, evidence and observed summaries can be collapsed using their keyboard-accessible headings.

## Invalid model suggestions

Provider instructions and the shared wire-schema description require a complete selection only for propose; clarify/analyze/cancel must return null. The backend always enforces this relationship locally, in strict-schema and JSON-object modes. Invalid selections remain rejected. AI request errors offer an explicit Retry AI message action rather than Reconnect. A retry sends a new model request and never executes or cancels a pipeline. There is no automatic retry or format fallback.

## Evidence readiness and limits

The evidence API computes analysis_ready from available parsed test results, independently of unknown test attempts and sharing settings. The assistant facts include evidence_complete, evidence_limitations, failure_details_shared, can_analyze_failure_details and analysis_limitations. background_analysis_pending is always false: there is no analysis job to wait for. Partial evidence supports only bounded conclusions from available shared failures. Missing or unsupported reports require a supported upload, failed parses require inspection, and disabled failure sharing requires changing Settings for detailed analysis. Unknown attempts limit rerun consistency claims; keyword retries do not supply test-attempt metadata. The UI displays these backend limitations separately from model prose. This does not establish root cause or verify operator-asserted artifact ownership.

## Bug report presentation

Requested drafts use plain text with a title and separate observations, impact, unverified hypotheses, evidence and follow-up sections. Provider instructions request actual newlines and one list item per line. The UI preserves whitespace, wraps long identifiers and displays model text through React escaping; it does not interpret HTML or Markdown. Reports remain drafts for review, with no publication action. Older one-line model output is displayed verbatim rather than inventing section boundaries. No clipboard action is added.
