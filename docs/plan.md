# Implementation backlog

## 0 — Foundation (implemented)
- Project registry for Robot API, mobile placeholder and Playwright.
- Real local Robot execution, report parsers, timeout/cancel, evidence and command UI.
- Automated checks for real Robot pass/fail, cancellation, timeout, invalid reports and API validation.

## 1 — Connect the three real projects
- Confirm mobile framework, repo locations, environment variables, suite/tag conventions and device requirements.
- Run one smoke suite per project; validate reports, screenshots and traces against source tools.
- Pin tested project dependencies and add configuration validation.
Acceptance: three real smoke runs with correctly linked evidence and reproducible setup instructions.

## 2 — Durable orchestration
- PostgreSQL run/project/conversation records; migrations.
- Remote runner polling, authentication, leases, heartbeats, cancellation acknowledgement and device locks.
- Log streaming, artifact manifest, retention and redaction.
Acceptance: reconnect/restart does not duplicate execution or silently lose state.

## 3 — Natural-language chat
- Model adapter and typed tools: list tests, create run, get evidence, cancel and rerun failures.
- Context builder, explicit environment/platform scope, plan preview and conversation persistence.
- Evaluate ambiguous instructions and cross-project context isolation.
Acceptance: user can ask for API plus web smoke and inspect two independently tracked runs.

## 4 — Evidence-based analysis
- Failure comparison, hypotheses labelled as hypotheses, draft bug reports.
- Requirement-to-existing-test mapping and coverage gaps.
Acceptance: every factual failure claim links to captured evidence.

## 5 — Test authoring and pilot
- Repository-aware draft tests, syntax validation, reviewable diffs and sandbox execution.
- Pilot with several QA engineers; measure triage time, suite-selection accuracy and manual steps saved.
- Add CI/TestRail integrations based on observed demand.
