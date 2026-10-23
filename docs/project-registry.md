# Pipeline project registry (schema version 1)

`projects.example.json` is a synthetic catalog for three stacks: `robot_api`, `robot_appium`, and `pytest_playwright`. Robot projects require `robot_xml`; pytest projects require `junit_xml`. The pipeline provider is Bitbucket. No company configuration or credential is included.

Copy the example to `projects.local.json` and set `RELAY_PROJECTS=projects.local.json` when starting the API. Keep credentials in a separate runtime secret store/environment (implemented by the future provider); this schema has no credential fields. Unknown fields are rejected. Do not put secrets in parameter values: the registry is intentionally exposed to the local UI.

Each project has a repository coordinate (`workspace/repository`), a default branch and allowed branches, enabled flag, report type and named pipelines. Each pipeline has a Bitbucket custom selector and parameter definitions. Parameter kinds describe environment, platform, team, test selector or workers. Values are explicit string allowlists with optional defaults; required parameters without defaults must be supplied. Mobile pipelines require a platform parameter with android/ios values. Deployment-specific upper-case values or fixed-platform pipelines should be mapped by the provider in a later issue.

## Local API

- `GET /api/project-registry`: validated catalog.
- `POST /api/plans/validate`: pure validation and plan snapshot. It does not connect to Bitbucket, persist a plan, authorize execution, or start a run.

Example request:

```json
{"project_id":"mobile","pipeline_id":"smoke","parameters":{"PLATFORM":"android"}}
```

Unknown projects, pipelines, branches, parameters, values, and missing required parameters return 422. Project defaults are resolved into a new snapshot without mutating the request or registry. The future execution endpoint must resolve/revalidate server-side and must not trust a client-provided snapshot.

## Contracts

`ExecutionPlan` holds the project snapshot, branch, pipeline selection and validated parameters. `Run` tracks `pipeline_status` independently from `test_status`; a successful pipeline may contain failed tests, or no collected evidence. `PipelineStep`, `TestResult`, and `Artifact` provide typed records for later monitoring and parser integration. Unknown retry counts stay null. Artifact storage keys are opaque references, not embedded signed URLs or credentials.

## Compatibility and scope

The existing `relay.example.json`, `/api/projects`, `/api/chat` and local JSON run storage remain the legacy local demo. Its Node Playwright adapter has not been migrated here; the new pipeline registry models web as pytest Python only. Do not enable the legacy Node adapter for a pytest repository. Remote trigger (#3), persisted runs (#4), and report parser integration (#5) will consume these contracts. These are schema contracts, not a state-transition engine or completed runtime integrations.
