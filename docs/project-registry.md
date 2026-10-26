# Pipeline project registry (schema version 1)

`projects.example.json` contains synthetic examples for Robot, Appium and pytest/Playwright. Framework is optional metadata: it does not constrain provider, parameters or reports. The MVP targets Bitbucket; accepting another provider identifier validates configuration only and does not implement that provider.

Copy the example to `projects.local.json` and set `RELAY_PROJECTS=projects.local.json` when starting the API. Keep credentials in a separate runtime secret store/environment (implemented by the future provider); this schema has no credential fields. Unknown fields are rejected. Do not put secrets in parameter values: the registry is intentionally exposed to the local UI.

Each project has a repository coordinate (`workspace/repository`), a default branch and allowed branches, enabled flag, a list of report descriptors and named pipelines. Each pipeline has a Bitbucket custom selector and parameter definitions. Parameter kinds are extensible UI metadata, such as environment, platform, browser, team, test selector or workers. Values are explicit string allowlists with optional defaults; required parameters without defaults must be supplied. A parameter is required only when its definition says so. Platform names and casing are configured per pipeline, including ANDROID/IOS or pipelines that need no platform input.

## Local API

- `GET /api/project-registry`: validated catalog.
- `POST /api/plans/validate`: pure validation and plan snapshot. It does not connect to Bitbucket, persist a plan, authorize execution, or start a run.

Example request:

```json
{"project_id":"mobile","pipeline_id":"smoke","parameters":{"PLATFORM":"android"}}
```

Unknown projects, pipelines, branches, parameters, values, and missing required parameters return 422. Project defaults are resolved into a new snapshot without mutating the request or registry. The future execution endpoint must resolve/revalidate server-side and must not trust a client-provided snapshot.

## Report descriptors

Each report declares a `format` parser identifier and a relative `path`. A project can have zero or multiple reports, independent of framework. Unknown formats remain valid: their artifacts can be tracked with `parse_status=unsupported` while run test status stays `not_collected` or `unknown`. Parser availability and provider availability must be checked by runtime adapters before use. This PR defines the contracts; it does not add parser dispatch or an artifact UI. Parent traversal and absolute paths are rejected; future download code must additionally enforce containment and symlink rules.

## Contracts

`ExecutionPlan` holds the project snapshot, branch, pipeline selection and validated parameters. `Run` tracks `pipeline_status` independently from `test_status`; a successful pipeline may contain failed tests, or no collected evidence. `PipelineStep`, `TestResult`, and `Artifact` provide typed records for later monitoring and parser integration. Unknown retry counts stay null. Test IDs and names preserve full nonempty source identities without the 256-character configuration-value limit; ingestion size limits belong at the report boundary. All registry validation completes before constructing the local runner, so invalid registry startup cannot rewrite saved run states. Artifact storage keys are opaque references, not embedded signed URLs or credentials.

## Compatibility and scope

The existing `relay.example.json`, `/api/projects`, `/api/chat` and local JSON run storage remain the legacy local demo. Its Node Playwright adapter has not been migrated here; the new pipeline registry models web as pytest Python only. Do not enable the legacy Node adapter for a pytest repository. Remote trigger (#3), persisted runs (#4), and report parser integration (#5) will consume these contracts. These are schema contracts, not a state-transition engine or completed runtime integrations.

### Optional SUT identity

A pipeline may declare up to 16 unique safe service IDs:

```json
"sut_ids": ["service-a", "service-b"]
```

Preview requests may select one via `sut_id`, or use `null` for automation-only
context. The selected ID must belong to that pipeline and is persisted in the
plan snapshot. It is not a Bitbucket pipeline variable and does not prove which
service version was deployed. An existing saved run cannot be retagged by changing
the registry or current UI selection. See [project knowledge](project-knowledge.md)
for category manifests, sharing consent and reviewed knowledge proposals.
