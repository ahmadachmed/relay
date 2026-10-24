# Bitbucket Cloud REST catalog

Relay reads repository metadata, verifies the configured default branch, and fetches
`bitbucket-pipelines.yml` directly from Bitbucket Cloud. The file is fetched at the
branch's resolved commit hash so a branch update cannot change the file mid-refresh.
The response includes `transport: rest`, `commit`, and `branch_scope: default_only`.
Only that branch is verified; this is not a complete branch listing.

## Setup / migration from MCP

1. Create a scoped Bitbucket API token with `read:repository:bitbucket`. Its account
   must have access to the repository. Use your Atlassian account email, not your
   Bitbucket username. Organization policies may restrict API-token access.
2. Set `RELAY_BITBUCKET_EMAIL` and `RELAY_BITBUCKET_API_TOKEN` privately in the
   backend terminal or secret manager. These are separate from Workbench credentials.
3. Keep your existing `projects.local.json`. Verify `repository`, `default_branch`,
   `allowed_branches`, and each curated pipeline `selector` against the actual YAML.
   Parameters remain administrator-owned; remote scripts and variable defaults are
   never exposed through the catalog.
4. Update dependencies and restart Relay from the repository root:

```bash
python -m pip install -e '.[dev]'
RELAY_PROJECTS=projects.local.json \
  python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
```

For macOS zsh, credentials can be entered without putting the token in shell history:

```zsh
read "RELAY_BITBUCKET_EMAIL?Atlassian account email: "
export RELAY_BITBUCKET_EMAIL
read -s "RELAY_BITBUCKET_API_TOKEN?Bitbucket API token: "
echo
export RELAY_BITBUCKET_API_TOKEN
```

Set these in the same terminal before starting the backend. `RELAY_MCP_CONFIG`,
`RELAY_WORKBENCH_API_KEY`, `mcp.local.json`, and MCP tool bindings are no longer used.
The MCP provider, examples, and SDK dependency have been removed. Existing private
MCP configuration remains ignored by Git but can be archived locally.

## Validate

```bash
curl -sS --max-time 70 http://127.0.0.1:8000/api/project-registry/api/catalog \
  | python -m json.tool
```

Replace `api` with your registry project ID. Require HTTP 200, `transport: rest`,
`connected: true`, the expected repository/branch, and `available: true` for each
intended selector. A valid file with a missing selector reports `available: false`;
check the exact custom pipeline name. The catalog does not run a pipeline.

The user's standalone live REST diagnostic returned HTTP 200, 221,117 file bytes,
valid YAML, and 108 custom pipelines. This verifies direct file access; the updated
Relay endpoint still needs an independent live check. Automated tests use synthetic
responses, including a file over 221 KB, and do not contain private repository data.

## Limits and diagnostics

Requests are GET-only and use a fixed `https://api.bitbucket.org` origin. Redirects
are rejected. Each decoded response is bounded to 2 MB while streaming; a refresh
has a 60-second overall network timeout. No automatic retry is performed. JSON is
used for metadata and raw UTF-8 YAML for the file, avoiding MCP gateway truncation.

Errors expose sanitized stages/codes rather than upstream bodies or credentials:
401 → `unauthorized`, 403 → `forbidden`, 404 → `not_found`, 429 → `rate_limited`.
Other unsuccessful statuses (including redirects) become `upstream_error`.
Malformed pipeline data fails without reporting a successful connection.

The catalog endpoint remains read-only. Confirmed trigger and manual reconciliation
are documented in [execution.md](execution.md). Remote status and cancellation are documented in
[monitoring.md](monitoring.md); artifacts remain subsequent work. Do not expose the local backend publicly;
it has no multi-user authentication. Credentials stay in the backend environment.

References:
- https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/
- https://developer.atlassian.com/cloud/bitbucket/rest/api-group-refs/
- https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/
