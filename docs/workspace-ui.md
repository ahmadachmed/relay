# Relay workspace UI

The Next.js / TypeScript workspace follows the [approved chat-first prototype](https://relay-qa-harness.amusedskunk2048.chatgpt.site/). It uses the existing remote execution API; it does not call an AI model.

## Run locally

Requires Node.js 20.9+ and the Python dependencies from the root README. From the repository root:

```bash
npm ci --prefix frontend
npm run build --prefix frontend
python -m uvicorn relay.api:create_app --factory --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/ui/. The root redirects there when the export exists. Restart the backend after the first build. The legacy local runner remains at `/legacy`; without a frontend export, `/` continues to serve the original runner. Rebuild after frontend edits. The production export and API share an origin; `next dev` alone does not proxy backend requests.

Configure projects and Bitbucket credentials on the backend as described in [providers](providers.md). The UI never asks for provider credentials. Keep this server local: authentication and multiple-user isolation are not implemented.

Use the header’s **Color theme** selector for Light, Dark, or System. System follows your operating system; your choice persists in this browser and is applied before the workspace renders.

## Workflow

1. Select a project, branch, pipeline, and parameters in Execution context. Choices come from the registry.
2. Preview the plan and review its repository, pinned commit, selector, and parameters. Editing context invalidates the preview.
3. Click **Confirm & run** to submit. The same plan cannot be submitted again through this page. If the response is lost, **Check status** reads the saved plan without re-posting it; do not create another plan until the existing submission is resolved.
4. Follow run progress from history. **Refresh remote status** asks the backend to query Bitbucket; normal polling reads saved observations. Cancel requires confirmation and distinguishes an acknowledged request from a verified stop.
5. Download the report from Bitbucket’s pipeline Artifacts tab, extract it locally, and upload the configured report file. Confirm that it belongs to the selected run. Pipeline status and test evidence are displayed separately. Missing evidence and unknown attempt counts remain explicit. Report slots are immutable.

The composer accepts `/preview`, `/status`, and `/help`. It does not execute natural-language instructions. Context, conversation, selected run, and unresolved submission ID are retained in this browser’s local storage. Raw report contents and provider credentials are not stored there. Avoid entering secrets into the conversation. Clearing browser storage removes this local continuity; the backend remains the source of run history.

## Validation

```bash
npm run build --prefix frontend
npm run typecheck --prefix frontend
cd frontend
npx playwright install --with-deps chromium
npm run test:e2e
```

Browser tests intercept API responses with synthetic fixtures. They exercise preview invalidation, single submission and recovery, cancellation confirmation, manual upload assertion, backend errors, and mobile layout. They do not start private pipelines. CI runs these alongside the Python suite.

The screenshot test writes `docs/screenshots/workspace-desktop-fixture.png` `workspace-plan-fixture.png`, and `workspace-mobile-fixture.png`. These are synthetic UI fixtures, not evidence from a real automation run. CI retains them as an artifact for visual review.
