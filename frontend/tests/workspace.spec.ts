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
  await page.getByLabel("Report file", { exact: true }).setInputFiles({
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

test("theme follows system, preserves explicit choice, and survives reload", async ({
  page,
}) => {
  await fixtures(page);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/ui/");
  const theme = page.getByRole("combobox", { name: "Color theme" });
  await expect(theme).toHaveValue("system");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await theme.selectOption("dark");
  await page.reload();
  await expect(theme).toHaveValue("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toBeVisible();
  fs.mkdirSync("../docs/screenshots", { recursive: true });
  await page.screenshot({
    path: "../docs/screenshots/workspace-dark-fixture.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await theme.selectOption("light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await theme.selectOption("system");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("failed recovery retains its action and blocks a new preview", async ({
  page,
}) => {
  await fixtures(page);
  await page.addInitScript(
    (id) => localStorage.setItem("relay.ui.attemptedPlan", id),
    id,
  );
  let previews = 0;
  await page.route("**/api/plans/preview", (r) => {
    previews++;
    return r.abort();
  });
  await page.route(`**/api/plans/${id}`, (r) =>
    r.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Temporary recovery error" }),
    }),
  );
  await page.goto("/ui/");
  await expect(page.getByText("Temporary recovery error")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Check status", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  expect(previews).toBe(0);
});

for (const code of ["stale_plan", "expired_plan"]) {
  test(`${code} permits fresh preview after reload`, async ({ page }) => {
    await fixtures(page);
    let previews = 0;
    await page.route("**/api/plans/preview", (r) => {
      previews++;
      return r.fulfill({
        contentType: "application/json",
        body: JSON.stringify(pinned),
      });
    });
    await page.route(`**/api/plans/${id}/execute`, (r) =>
      r.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code, message: "Preview again" } }),
      }),
    );
    await page.goto("/ui/");
    await page
      .getByRole("button", { name: "Preview plan", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Confirm & run", exact: true })
      .click();
    await expect(
      page.getByRole("alert").filter({ hasText: "Preview again" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Check status", exact: true }),
    ).toHaveCount(0);
    expect(
      await page.evaluate(() => localStorage.getItem("relay.ui.attemptedPlan")),
    ).toBe("");
    await page.reload();
    await page
      .getByRole("button", { name: "Preview plan", exact: true })
      .click();
    await expect.poll(() => previews).toBe(2);
  });
}

test("expired unclaimed recovery unlocks a fresh preview", async ({ page }) => {
  await fixtures(page);
  await page.addInitScript(
    (id) => localStorage.setItem("relay.ui.attemptedPlan", id),
    id,
  );
  await page.route(`**/api/plans/${id}`, (r) =>
    r.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ...pinned,
        expired_unclaimed: true,
        expires_at: Date.now() / 1000 - 60,
      }),
    }),
  );
  await page.goto("/ui/");
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => localStorage.getItem("relay.ui.attemptedPlan")),
  ).toBe("");
});

test("failed older-history request retries the same offset", async ({
  page,
}) => {
  await fixtures(page);
  const offsets: number[] = [];
  await page.route("**/api/remote-runs**", (r) => {
    const u = new URL(r.request().url());
    if (u.pathname != "/api/remote-runs") return r.fallback();
    const offset = Number(u.searchParams.get("offset") || 0);
    offsets.push(offset);
    if (offset === 0)
      return r.fulfill({
        contentType: "application/json",
        body: JSON.stringify(
          Array.from({ length: 30 }, (_, i) => ({
            ...run,
            id: String(i).padStart(32, "0"),
          })),
        ),
      });
    if (offsets.length === 2)
      return r.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ detail: "History temporarily unavailable" }),
      });
    return r.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.goto("/ui/");
  await page
    .getByRole("button", { name: "Load older runs", exact: true })
    .click();
  await expect(page.getByText("History temporarily unavailable")).toBeVisible();
  await page
    .getByRole("button", { name: "Load older runs", exact: true })
    .click();
  await expect.poll(() => offsets).toEqual([0, 30, 30]);
});

test("AI is opt-in and proposals invalidate the previous plan without executing", async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.route("**/api/assistant", (r) =>
    r.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ enabled: true, configured: true }),
    }),
  );
  let aiCalls = 0;
  await page.route("**/api/assistant/chat", (r) => {
    aiCalls++;
    expect(r.request().postDataJSON().share_context).toBe(true);
    expect(r.request().postDataJSON().include_failure_details).toBe(false);
    return r.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        run_id: null,
        facts: null,
        limitations: ["AI suggestions can be wrong."],
        answer: {
          action: "propose",
          message: "Try dev regression on develop.",
          selection: {
            project_id: "api",
            pipeline_id: "smoke",
            branch: "develop",
            parameters: { ENV: "dev", TAGS: "regression" },
          },
          hypotheses: [],
          bug_report_draft: null,
        },
      }),
    });
  });
  await page.goto("/ui/");
  const command = page.getByRole("textbox", { name: "Structured command" });
  await command.fill("run regression on dev");
  await page.getByRole("button", { name: "Send command" }).click();
  expect(aiCalls).toBe(0);
  await page.getByRole("button", { name: "Preview plan", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toBeVisible();
  await page
    .getByLabel(
      "Send chat, recent conversation, configured context and selected run summary to AI",
    )
    .check();
  await command.fill("run regression on dev");
  await page.getByRole("button", { name: "Send command" }).click();
  await expect(
    page.getByRole("region", { name: "AI suggestion" }),
  ).toBeVisible();
  await expect(page.getByRole("region",{name:"Execution plan"})).toHaveCount(0);
  fs.mkdirSync("../docs/screenshots", {recursive:true});
  await page.setViewportSize({width:1440,height:1400});
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:"../docs/screenshots/ai-proposal-fixture.png"});
  await page.getByRole("button", { name: "Use proposed context" }).click();
  await expect(
    page.getByRole("region", { name: "Execution plan" }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Branch", { exact: true })).toHaveValue(
    "develop",
  );
  await expect(page.getByLabel("ENV", { exact: true })).toHaveValue("dev");
  expect(state.counts().executions).toBe(0);
});

test("AI cancellation only opens the existing explicit confirmation", async ({
  page,
}) => {
  const state = await fixtures(page, true);
  await page.route("**/api/assistant", (r) =>
    r.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ enabled: true, configured: true }),
    }),
  );
  await page.route("**/api/assistant/chat", (r) =>
    r.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        run_id: id,
        facts: null,
        limitations: [],
        answer: {
          action: "cancel",
          message: "Review the stop request.",
          selection: null,
          hypotheses: [],
          bug_report_draft: null,
        },
      }),
    }),
  );
  await page.goto("/ui/");
  await page.getByRole("button", { name: /api/ }).first().click();
  await page
    .getByLabel(
      "Send chat, recent conversation, configured context and selected run summary to AI",
    )
    .check();
  await page
    .getByRole("textbox", { name: "Structured command" })
    .fill("stop this run");
  await page.getByRole("button", { name: "Send command" }).click();
  await page.getByRole("button", { name: "Review cancellation" }).click();
  await expect(
    page.getByRole("button", { name: "Confirm cancellation", exact: true }),
  ).toBeVisible();
  expect(state.counts().cancellations).toBe(0);
});

test("AI provider error leaves commands usable on mobile", async ({ page }) => {
  await fixtures(page);
  await page.route("**/api/assistant", (r) =>
    r.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ enabled: true, configured: true }),
    }),
  );
  await page.route("**/api/assistant/chat", (r) =>
    r.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Model unavailable" }),
    }),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/");
  await page
    .getByLabel(
      "Send chat, recent conversation, configured context and selected run summary to AI",
    )
    .check();
  await page
    .getByRole("textbox", { name: "Structured command" })
    .fill("analyze");
  await page.getByRole("button", { name: "Send command" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Model unavailable" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("textbox", { name: "Structured command" }).fill("/help");
  await page.getByRole("button", { name: "Send command" }).click();
  await expect(page.getByText(/Available commands:/)).toBeVisible();
});
