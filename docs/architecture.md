# Architecture

The local vertical slice is: chat command → FastAPI → configured project/suite → runner → report parser → normalized run and evidence.

A project identifies an adapter, local checkout, enabled flag, timeout and named suites. Users can choose configured project/suite identifiers; they cannot submit arbitrary shell commands. All three automation projects can live in their existing repositories. Relay is the orchestration repository.

A run stores ID, project, suite, timestamps, status, exit code, tests and error. Status moves from queued to running, then passed/failed/error/cancelled. Interrupted identifies a run whose server exited before completion. Test outcomes include pass/fail/skip/flaky/unknown. Playwright expected failures are evaluated using the report's expected/unexpected outcome rather than raw attempt status.

MVP simplification: execution and API share one Python process, one execution slot, and local atomic JSON files. This is intentionally smaller than the proposed hosted architecture. Before hosting, split a durable job queue from runner execution, add leases/heartbeats and device locks, implement authenticated runner polling, move metadata to PostgreSQL and artifacts to object storage.

Future AI orchestration should call typed tools over the same run service. Build project context from a test catalog, requirements and selected run evidence. Keep factual results deterministic and label inferred causes. Add redaction and explicit context selection before any external model receives logs.
