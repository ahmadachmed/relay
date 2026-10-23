# Relay

A chat-first QA harness for existing automation projects: Robot API, mobile automation, and Playwright web.

This initial foundation runs real local suites and normalizes their evidence. The chat currently accepts explicit commands; it does **not** use an AI model. The bundled Robot suite is a local example, not a real API integration.

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
