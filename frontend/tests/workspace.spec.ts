import { test, expect, Page } from "@playwright/test";
import fs from "node:fs";

const parameters = {
  ENV: {
    kind: "environment",
    allowed_values: ["uat", "dev"],
    required: true,
    default: "uat",
  },
  TAGS: {
    kind: "tags",
    allowed_values: ["smoke", "regression"],
    required: true,
    default: "smoke",
  },
};
const project = {
  provider: "bitbucket",
  framework: "robot_api",
  repository: "example/api-automation",
  default_branch: "main",
  allowed_branches: ["main", "develop"],
  enabled: true,
  pipelines: { smoke: { selector: "smoke-tests", parameters } },
  reports: [{ format: "robot_xml", path: "output.xml" }],
};
const id = "a".repeat(32);
const pinned = {
  id,
  commit: "b".repeat(40),
  selector: "smoke-tests",
  status: "preview",
  expires_at: Date.now() / 1000 + 900,
  error: null,
  pipeline: null,
  plan: {
    project_id: "api",
    pipeline_id: "smoke",
    branch: "main",
    parameters: { ENV: "uat", TAGS: "smoke" },
    project,
  },
};
const run = {
  id,
  project_id: "api",
  pipeline: {
    uuid: "{12345678-1234-1234-1234-123456789abc}",
    url: "https://bitbucket.org/example/api-automation/pipelines/results/1",
  },
  pipeline_status: "running",
  test_status: "not_collected",
  terminal: false,
  monitoring_status: "active",
  last_checked_at: Date.now() / 1000,
  error: null,
  cancel_status: "none",
  cancel_error: null,
  steps: [
    { id: "step-one", name: "API automation", status: "running", result: null },
  ],
};
const missing = {
  run_id: id,
  test_status: "incomplete",
  analysis_ready: false,
  artifacts: [
    {
      name: "output.xml",
      format: "robot_xml",
      availability: "missing",
      parse_status: "not_attempted",
    },
  ],
  tests: [],
};

async function fixtures(page: Page, history = false) {
  let submitted = history,
    uploaded = false,
    cancelled = false,
    executions = 0,
    cancellations = 0,
    uploads = 0;
  const messages: string[] = [];
  page.on("pageerror", (error) => messages.push(error.message));
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      u = new URL(req.url()),
      path = u.pathname;
    const respond = (data: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    const current = {
      ...run,
      cancel_status: cancelled ? "acknowledged" : "none",
    };
    if (path === "/api/project-registry")
      return respond({ schema_version: 1, projects: { api: project } });
    if (path === "/api/remote-runs") return respond(submitted ? [current] : []);
    if (path === "/api/plans/preview") {
      const body = req.postDataJSON();
      return respond({
        ...pinned,
        plan: {
          ...pinned.plan,
          branch: body.branch,
          parameters: body.parameters,
        },
      });
    }
    if (path === `/api/plans/${id}/execute`) {
      executions++;
      submitted = true;
      expect(req.postDataJSON()).toEqual({ confirmed: true });
      return respond({
        ...pinned,
        status: "submitted",
        pipeline: run.pipeline,
      });
    }
    if (path === `/api/plans/${id}`)
      return respond({
        ...pinned,
        status: submitted ? "submitted" : "preview",
        pipeline: submitted ? run.pipeline : null,
      });
    if (path === `/api/remote-runs/${id}`) return respond(current);
    if (path === `/api/remote-runs/${id}/refresh`) return respond(current);
    if (path === `/api/remote-runs/${id}/cancel`) {
      cancellations++;
      cancelled = true;
      return respond({ ...run, cancel_status: "acknowledged" });
    }
    if (path === `/api/remote-runs/${id}/evidence` && req.method() === "POST") {
      uploads++;
      uploaded = true;
      return respond({ id: "artifact-one", parse_status: "parsed" });
    }
    if (path === `/api/remote-runs/${id}/evidence`)
      return respond(
        uploaded
          ? {
              ...missing,
              test_status: "failed",
              artifacts: [
                {
                  id: "artifact-one",
                  name: "output.xml",
                  format: "robot_xml",
                  availability: "available",
                  parse_status: "parsed",
                  source: "manual_upload",
                  association: "operator_asserted",
                },
              ],
              tests: [
                {
                  id: "test-one",
                  name: "Example login test",
                  status: "fail",
                  message: "Expected 200; received 500",
                  artifact_ids: ["artifact-one"],
                  attempts: null,
                },
              ],
            }
          : missing,
      );
    return respond({ detail: "Fixture endpoint not found" }, 404);
  });
  return { counts: () => ({ executions, cancellations, uploads }), messages };
}

test("preview is read-only, context edits invalidate it, explicit execution submits once", async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto("/ui/");
  await expect(
    page.getByRole("heading", { name: "What are we testing today?" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toBeVisible();
  expect(state.counts().executions).toBe(0);
  await page.getByLabel("Branch", { exact: true }).selectOption("develop");
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toContainText("develop");
  await page
    .getByRole("button", { name: "Confirm & run", exact: true })
    .dblclick();
  await expect(
    page.getByRole("region", { name: "Selected run" }),
  ).toContainText("API automation");
  expect(state.counts().executions).toBe(1);
  await page.reload();
  await expect(
    page.getByRole("region", { name: "Selected run" }),
  ).toContainText("API automation");
  expect(state.counts().executions).toBe(1);
  expect(state.messages).toEqual([]);
});

test("cancel requires confirmation and keeps acknowledged distinct from stopped", async ({
  page,
}) => {
  const state = await fixtures(page, true);
  await page.goto("/ui/");
  await page.getByRole("button", { name: /api/ }).first().click();
  await page
    .getByRole("button", { name: "Cancel pipeline", exact: true })
    .click();
  expect(state.counts().cancellations).toBe(0);
  await page
    .getByRole("button", { name: "Confirm cancellation", exact: true })
    .click();
  await expect(page.getByText("Cancellation:", { exact: false })).toContainText(
    "acknowledged",
  );
  await expect(
    page.getByRole("region", { name: "Selected run" }),
  ).toContainText("running");
  expect(state.counts().cancellations).toBe(1);
});

test("manual evidence upload requires a run assertion and exposes factual results", async ({
  page,
}) => {
  const state = await fixtures(page, true);
  await page.goto("/ui/");
  await page.getByRole("button", { name: /api/ }).first().click();
  const area = page.getByRole("region", { name: "Test evidence" });
  await expect(area).toContainText("missing");
  await page
    .getByLabel("Report file", { exact: true })
    .setInputFiles({
      name: "output.xml",
      mimeType: "application/xml",
      buffer: Buffer.from("<robot/>"),
    });
  await expect(
    page.getByRole("button", { name: "Upload evidence", exact: true }),
  ).toBeDisabled();
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Upload evidence", exact: true })
    .click();
  await expect(area).toContainText("Example login test");
  await area.getByText("Example login test", { exact: true }).click();
  await expect(area).toContainText("Expected 200; received 500");
  await expect(area).toContainText("Attempts: unknown");
  expect(state.counts().uploads).toBe(1);
});

test("ambiguous execute recovers by reading the original plan and never reposts", async ({
  page,
}) => {
  const state = await fixtures(page);
  let posts = 0;
  await page.route(`**/api/plans/${id}/execute`, async (route) => {
    posts++;
    await route.abort("failed");
  });
  await page.goto("/ui/");
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await page
    .getByRole("button", { name: "Confirm & run", exact: true })
    .click();
  await expect(page.getByText("Check the saved submission")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Check the saved submission")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Confirm & run", exact: true }),
  ).toBeDisabled();
  expect(posts).toBe(1);
});

test("backend errors and mobile layout are visible without horizontal overflow", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/plans/preview", (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { code: "forbidden", message: "Bitbucket access denied" },
      }),
    }),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/");
  await page.getByRole("button", { name: "Toggle execution context" }).click();
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await page.getByRole("button", { name: "Close execution context" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Bitbucket access denied" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("capture reference screenshots with explicitly synthetic fixtures", async ({
  page,
}) => {
  await fixtures(page);
  await page.goto("/ui/");
  await expect(
    page.getByRole("heading", { name: "What are we testing today?" }),
  ).toBeVisible();
  fs.mkdirSync("../docs/screenshots", { recursive: true });
  await page.screenshot({
    path: "../docs/screenshots/workspace-desktop-fixture.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "../docs/screenshots/workspace-mobile-fixture.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toBeVisible();
  await page.screenshot({
    path: "../docs/screenshots/workspace-plan-fixture.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole("button", { name: "Confirm & run", exact: true })
    .scrollIntoViewIfNeeded();
  await page
    .getByRole("button", { name: "Confirm & run", exact: true })
    .click({ trial: true });
});
