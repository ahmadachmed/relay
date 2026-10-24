# Execution providers and connections

A provider identifies the system that runs automation; its transport is an implementation detail. `CatalogProvider` supplies the read-only discovery contract in this increment. Bitbucket Cloud is the first implementation, using REST. A future MCP client can implement the same catalog contract, but the ChatGPT MCP connection is not available to the Relay backend automatically. Trigger, monitoring, cancellation and artifact operations belong to issues #3–#5.

## Configure Bitbucket Cloud

Create a scoped Bitbucket API token with repository read permission (`read:repository:bitbucket`) and access to the selected repository. The backend uses Basic authentication with the Atlassian account email and API token. Set `RELAY_BITBUCKET_EMAIL` and `RELAY_BITBUCKET_API_TOKEN` in the process environment or your secret manager. Never put them in the project registry or commit them. Reference: https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/

Copy `projects.example.json` to `projects.local.json`, set the repository, allowed branches and custom pipeline selectors, and launch with `RELAY_PROJECTS=projects.local.json`. Request `GET /api/project-registry/{project_id}/catalog` to refresh. Each request checks access, follows branch pagination, and reads `bitbucket-pipelines.yml` at the configured default branch.

Only configured pipelines and parameter definitions are returned. `available=false` means a configured selector is missing from the remote YAML. Remote variable defaults and scripts are never returned. Parameters are currently curated in the validated local registry, not automatically imported. YAML is parsed as data with SafeLoader and a 2 MB response limit. Catalog describes the default branch only; execution must verify the selected branch again before triggering.

Missing credentials, denied access, missing resources, network failures and rate limits produce stable error codes and sanitized messages. No automatic retry is performed; refresh after correcting the problem or waiting for rate limits. Pagination is capped at 50 pages and restricted to the repository's branch endpoint. Redirects are rejected to prevent credentials being forwarded elsewhere.

## Validation boundary

Contract tests use mocked HTTP responses. Live authentication and a real sandbox catalog must still be validated by running Relay with backend credentials. ChatGPT's connected Atlassian account is not proof that Relay's independent connection works. No pipeline is triggered by catalog refresh. Other provider identifiers are accepted by the registry but the endpoint returns 501 until their implementations are installed. The existing local-only server restrictions still apply.
