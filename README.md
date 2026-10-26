# Relay

A chat-first QA harness for existing automation projects: Robot API, mobile automation, and Playwright web.

This initial foundation runs real local suites and normalizes their evidence. Structured commands work without AI; the remote workspace also offers an optional, explicitly enabled AI assistant. The bundled Robot suite is a local example, not a real API integration.

## Start

Requires Python 3.11+ on Linux/macOS. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and send:

```text
run robot-api smoke
run robot-api failure-demo
status <run-id>
cancel <run-id>
```

Run validation with `python -m pytest -q`.

## Connect your projects

Copy `relay.example.json` to `relay.local.json`, set local checkout paths and suite arguments, then start with:

```bash
RELAY_CONFIG=relay.local.json python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
```

Paths resolve relative to the configuration file. Suite arguments are administrator-owned configuration, not chat input. The harness invokes commands with argument arrays and no shell.

- **Robot API:** replace the example path and suite arguments with your existing tests. Install their requirements in the runner environment.
- **Mobile:** intentionally disabled with adapter `unconfigured`. Confirm the mobile framework first. Robot/Appium projects can use the Robot adapter after installing their drivers, libraries and configuring an available device. No devices are provisioned by Relay.
- **Web Playwright:** point at an existing Node project, install its dependencies and browsers, then enable it. The adapter invokes the locally installed Playwright using `npx --no-install`, forces the JSON reporter, and stores browser artifacts in the run directory.

## Current capabilities

- Local chat command interface, project catalog and run history.
- Single active run, cancellation and timeout; Linux/macOS process-group termination.
- Atomic JSON persistence in `.relay/`; incomplete jobs are marked interrupted on restart.
- Robot XML and Playwright JSON parsing, including Playwright retries/flaky outcomes.
- Evidence downloads and separate execution errors versus assertion failures.

## Boundaries

This is a local development foundation, not a hosted multi-user service. Run **one server process**, without auto-reload while tests are active. Do not expose it publicly: authentication, tenant isolation and a remote runner protocol are not implemented. Configure only trusted test repositories: their tests execute code with your local permissions. An interrupted server may leave a test/device process to clean up before another run.

Report files and console logs may contain secrets. They stay on the local machine; redaction is not implemented, and no data is sent to a model. Keep `.relay/`, local configuration, credentials and environment files out of Git. Screenshots/traces remain in the artifact directory; the initial UI exposes report and console downloads only.

The Playwright parser is fixture-tested; a live Playwright browser run has not yet been verified. Windows process-tree cancellation is not supported. Persistent chat history, natural-language planning, rerun-failed, PostgreSQL, SSE, remote runners and integrations are roadmap items.

See [architecture](docs/architecture.md) and [implementation plan](docs/plan.md).

## Pipeline registry foundation

The new, separate [project registry](docs/project-registry.md) uses independent provider, configurable parameters and multiple report descriptors. Framework is optional metadata; examples cover Robot API, Robot/Appium and **pytest + Playwright Python**. Start with `projects.example.json` and validate plans at `/api/plans/validate`. This endpoint has no execution side effects. The original local demo remains compatible; its legacy Node Playwright adapter is not suitable for pytest projects. Bitbucket catalog connection is available; remote execution remains follow-up work.

## Bitbucket REST catalog connection

See [Bitbucket setup](docs/providers.md) for API-token authentication and migration from MCP.
`GET /api/project-registry/{project_id}/catalog` verifies the configured default branch and reads pipeline declarations directly from Bitbucket Cloud at its resolved commit. No clone or MCP configuration is required. Pipeline triggers are not implemented.

Confirmed remote preview/trigger and recovery: [execution API](docs/execution.md).

Remote run history, polling and cancel: [monitoring API](docs/monitoring.md).

Report upload and evidence parsing: [evidence API](docs/evidence.md).

## Chat-first remote workspace

Build the Next.js frontend and serve it from the Python backend: see [workspace UI setup and testing](docs/workspace-ui.md). It provides registry-based preview, confirmed execution, progress, cancellation, and manual evidence upload.

## Optional AI conversation

See [assistant setup, sharing controls and limits](docs/assistant.md) and [private project knowledge setup](docs/project-knowledge.md). AI proposes configured context and analyzes evidence; execution and cancellation retain explicit UI confirmation. Disabled by default.

Approved automation/SUT knowledge can be shared independently. AI knowledge
proposals remain private drafts until exact-content review and explicit approval;
unverified storage never activates a hypothesis. See the [local knowledge acceptance
steps and revision/rollback workflow](docs/project-knowledge.md#reviewed-ai-proposals-revisions-and-rollback).
