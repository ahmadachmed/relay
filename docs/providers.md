# MCP execution-provider connection

Relay connects directly to a remote MCP server through the official Python MCP SDK (v1 API, pinned below v2), using Streamable HTTP. The backend initializes a session, paginates `tools/list`, validates configured arguments against advertised schemas, and calls only explicitly configured tools. There are no direct Bitbucket REST requests. The initial provider still understands Bitbucket custom pipeline YAML; other source/CI formats need another catalog adapter.

## Setup

1. Copy `mcp.example.json` to ignored `mcp.local.json`.
2. Set your server URL. HTTPS is required except for localhost. The example tool names are placeholders, not verified Workbench or Atlassian names.
3. Map the three read operations to actual server tool names and arguments: repository lookup, branch listing, file reading. For gateway tools, nested arguments are supported. Only map operations you have verified are read-only: tool annotations alone are not an authorization boundary.
4. Map response fields with dotted `result_path` and optional `cursor_path`. Repository must yield a nonempty object, branches a list of objects with `name`, file read a string containing YAML. Cursor must be null/empty on the final page. If the server omits its terminal cursor, set `missing_cursor_is_end: true` on the branches binding. The default is strict so missing fields cannot silently truncate catalogs. Omit `cursor_path` only for a tool that returns all branches in one response; the adapter cannot infer unreported truncation.
5. Set `RELAY_MCP_CONFIG=mcp.local.json` in the backend environment. Set each secret environment variable referenced by `header_env`, such as `RELAY_WORKBENCH_API_KEY`. Literal secrets do not belong in this file. For bearer auth, map `Authorization` to an environment variable containing the complete `Bearer ...` value.
6. Configure the project's repository, branches, pipeline selectors and parameters in `projects.local.json`, set `RELAY_PROJECTS`, then request `GET /api/project-registry/{project_id}/catalog`.

Available argument placeholders are `$workspace`, `$repo`, `$repository`, `$branch`, `$cursor`. The initial `$cursor` dictionary argument is omitted by default, including nested objects, so optional string schemas accept the first request. Set `omit_initial_cursor: false` on a binding if the server explicitly needs null. Subsequent cursors are passed unchanged. Only whole-string placeholders are substituted; nested dictionaries/lists are supported. Responses use MCP structuredContent, falling back to a single JSON text content block. Set result paths to match your server's actual response envelope.

## Authentication and support boundaries

Custom headers/API keys and externally supplied bearer credentials are supported. Interactive OAuth login, token refresh, stdio and legacy SSE transports are not implemented. Do not assume the Atlassian OAuth server works until that flow is added or an appropriate supported credential is configured. ChatGPT's MCP connection cannot be reused as Relay's authentication.

A fresh session is initialized and closed per refresh with an overall timeout. Redirects are disabled. Tools/branch pagination is bounded; returned tool data is capped at 2 MB after SDK decoding (this is not a wire-level memory limit). Errors are sanitized rather than exposing remote messages or credentials. Tool names/schemas must be checked at the configured server; arbitrary tools advertised by the server are never automatically invoked.

Only curated project parameter definitions and configured pipeline availability are returned, never source scripts or remote variable defaults. No pipeline is triggered. Catalog is checked on the default branch; execution must revalidate the chosen branch in #3. Unsupported parsers/providers remain future work.

## Validation

Contract tests cover discovery, pagination, schema mismatch, text/structured responses, secret environment headers, tool failures and invalid URLs. Live server authentication and tool mapping remain pending until a Relay runtime connection is supplied. Draft PR/issue #2 must stay open until a sandbox catalog refresh succeeds through this independent MCP connection.

SDK reference: https://github.com/modelcontextprotocol/python-sdk

Nested SDK ExceptionGroup errors preserve known ProviderError codes and timeout classification; unknown exception messages remain sanitized.

HTTP failures, including those nested inside SDK task groups, retain sanitized diagnostic codes: 401 → `unauthorized`, 403 → `forbidden`, 429 → `rate_limited`, other unsuccessful HTTP statuses → `upstream_error`. Relay returns these codes under `detail.code` (502 for upstream auth/access errors, 503 for rate limits). Remote URLs, headers and response bodies are never included. No automatic retry is performed.

## Official Atlassian MCP profile

Copy `mcp.atlassian.example.json` to ignored `mcp.local.json`. It uses `https://mcp.atlassian.com/v2/mcp`, with personal API-token Basic auth assembled in memory from `RELAY_ATLASSIAN_EMAIL` and `RELAY_ATLASSIAN_API_TOKEN`. Set those variables in the terminal/secret manager before starting Relay. Organization admins must permit API-token MCP authentication; a personal token is not a service-account bearer API key. Interactive OAuth remains unsupported.

The profile maps `executeRead` to `getBitbucketRepository`, `getBitbucketRepoBranch` and `getBitbucketRepoFileContent`, using the operation names/input coordinates discovered from the connected Atlassian tool catalog. Responses are mapped from the `data` envelope. The SDK validates the advertised outer tool schema. Actual tool availability/scopes may vary by authentication/account: a live Relay session must verify the profile before treating it as production-ready.

Branch lookup is explicitly `result_shape: single`: it verifies only the configured default branch and returns `branch_scope: default_only`. It does not claim to enumerate all repository branches. A normal listing binding returns `branch_scope: listed`.

Start from the repository root:

```bash
cp mcp.atlassian.example.json mcp.local.json
cp projects.example.json projects.local.json
# Edit projects.local.json with your repository, branch and selectors.
# Set RELAY_ATLASSIAN_EMAIL and RELAY_ATLASSIAN_API_TOKEN privately.
RELAY_MCP_CONFIG=mcp.local.json RELAY_PROJECTS=projects.local.json \
  python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
```

Then request `GET /api/project-registry/api/catalog`. A successful independent runtime response requires `connected: true`, the intended default branch and `available: true` for the selected pipeline. No pipeline is triggered. The connected ChatGPT Atlassian session verified the operation catalog and branch envelope, but it is not a substitute for this runtime validation.

Official setup/authentication references:
- https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/
- https://support.atlassian.com/atlassian-ai-gateway/docs/configure-authentication-via-api-token/
