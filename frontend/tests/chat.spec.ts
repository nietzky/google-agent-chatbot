import { test, expect, type Page } from "@playwright/test";

async function mockAPI(page: Page, fail = false, slow = false) {
  let conversations: unknown[] = [],
    messages: unknown[] = [];
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    let data: unknown = {};
    if (path === "/api/memories") data = [];
    else if (path === "/api/memories/status") data = { enabled: true, jobs: {} };
    else if (path === "/api/products")
      data = {
        available: true,
        products: [
          {
            id: "s3",
            name: "Amazon Simple Storage Service",
            docs_url: "https://docs.aws.amazon.com/s3/",
            chunks: 45,
          },
          {
            id: "ec2",
            name: "Amazon Elastic Compute Cloud",
            docs_url: "https://docs.aws.amazon.com/ec2/",
            chunks: 20,
          },
        ],
      };
    else if (path === "/api/conversations") {
      if (method === "POST") {
        const created = {
          id: "one",
          title: route.request().postDataJSON().title,
          updated_at: 1,
        };
        conversations = [created];
        data = created;
      } else data = conversations;
    } else if (path.endsWith("/messages")) {
      if (method === "POST") {
        if (fail)
          return route.fulfill({
            status: 503,
            json: { detail: "The assistant is temporarily unavailable." },
          });
        if (slow) await new Promise((resolve) => setTimeout(resolve, 1500));
        const answer = {
          id: "answer-1",
          role: "assistant",
          text: "Check your IAM policy. [AWS documentation](https://docs.aws.amazon.com/s3/)",
        };
        messages = [
          {
            id: "question-1",
            role: "user",
            text: route.request().postDataJSON().text,
          },
          answer,
        ];
        return route.fulfill({
          contentType: "text/event-stream",
          body: `event: message\ndata: ${JSON.stringify(answer)}\n\nevent: done\ndata: {}\n\n`,
        });
      }
      data = messages;
    } else if (method === "DELETE") {
      conversations = [];
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ json: data });
  });
}

test("send, cite, and restore a conversation after reload", async ({
  page,
}) => {
  await mockAPI(page);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Let’s get things working." }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Message Pingo" })
    .fill("S3 AccessDenied");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(
    page.getByRole("link", { name: "AWS documentation" }),
  ).toHaveAttribute("href", "https://docs.aws.amazon.com/s3/");
  await page.reload();
  await expect(page.getByRole("complementary", { name: "Conversation sidebar" })).toBeVisible();
  await page.getByRole("button", { name: /^S3 AccessDenied/ }).click();
  await expect(
    page.getByRole("link", { name: "AWS documentation" }),
  ).toBeVisible();
});

test("product search scopes the next turn and can be cleared", async ({
  page,
}) => {
  await mockAPI(page);
  await page.goto("/");
  await page
    .getByRole("button", { name: "Supported products", exact: true })
    .click();
  await page.getByRole("textbox", { name: "Search products" }).fill("s3");
  await expect(
    page.getByRole("button", { name: /Amazon Elastic Compute/ }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: /Amazon Simple Storage/ }).click();
  await expect(page.getByText("Discussing")).toBeVisible();
  await page
    .getByRole("textbox", { name: "Message Pingo" })
    .fill("I get AccessDenied");
  const request = page.waitForRequest(
    (r) => r.url().endsWith("/messages") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Send message" }).click();
  expect((await request).postDataJSON().product_id).toBe("s3");
  await expect(
    page.getByRole("link", { name: "AWS documentation" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Clear selected product" }).click();
  await expect(page.getByText("Discussing")).toHaveCount(0);
});

test("failed requests display recoverable error", async ({ page }) => {
  await mockAPI(page, true);
  await page.goto("/");
  await page.getByRole("textbox", { name: "Message Pingo" }).fill("Hello");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "temporarily unavailable",
  );
  await expect(
    page.getByRole("textbox", { name: "Message Pingo" }),
  ).toBeEnabled();
});

test("stop cancels an in-flight turn", async ({ page }) => {
  await mockAPI(page, false, true);
  await page.goto("/");
  await page.getByRole("textbox", { name: "Message Pingo" }).fill("Hello");
  await page.getByRole("button", { name: "Send message" }).click();
  await page.getByRole("button", { name: "Stop response" }).click();
  await expect(page.getByRole("alert")).toContainText("Response stopped");
});

test("mobile product picker fits and Escape closes it", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockAPI(page);
  await page.goto("/");
  await page
    .getByRole("button", { name: "Supported products", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
});

test("saved application context is grouped, product-filtered, and refreshed", async ({ page }) => {
  await mockAPI(page);
  let version = "Python 3.12";
  await page.route("**/api/memories", (route) => route.fulfill({ json: [
    { key: "runtime", value: version, subject: "payments-api", category: "environment", product_id: "s3" },
    { key: "worker", value: "Background processing on EC2", subject: "worker", category: "application", product_id: "ec2" },
    { key: "preference", value: "Prefer CLI examples", subject: "user", category: "preference" },
  ] }));
  await page.goto("/");
  const context = page.getByRole("region", { name: "Application context" });
  await context.locator("summary").click();
  await expect(context.getByText("Python 3.12")).toBeVisible();
  await expect(context.getByRole("heading", { name: "payments api" })).toBeVisible();
  await page.getByRole("button", { name: "Supported products", exact: true }).click();
  await page.getByRole("button", { name: /Amazon Simple Storage/ }).click();
  await expect(context.getByText("Background processing on EC2")).toHaveCount(0);
  await expect(context.getByText("Prefer CLI examples")).toBeVisible();
  version = "Python 3.13";
  await context.getByRole("button", { name: "Refresh" }).click();
  await expect(context.getByText("Python 3.13")).toBeVisible();
  await expect(context.getByText("Python 3.12")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("context learns after replies and fails without blocking the composer", async ({ page }) => {
  await mockAPI(page);
  let learned = false;
  let fail = false;
  await page.route("**/api/memories", (route) => route.fulfill(fail
    ? { status: 503, json: {} }
    : { json: learned ? [{ key: "app", value: "Uses Lambda", subject: "payments-api", category: "product_usage" }] : [] }));
  await page.goto("/");
  const context = page.getByRole("region", { name: "Application context" });
  await expect(context).toContainText("No saved application context yet");
  learned = true;
  await page.getByRole("textbox", { name: "Message Pingo" }).fill("My payments API uses Lambda");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(context).toContainText("1 saved detail");
  await context.locator("summary").click();
  await expect(context.getByText("Uses Lambda")).toBeVisible();
  fail = true;
  await context.getByRole("button", { name: "Refresh" }).click();
  await expect(context).toContainText("Saved context is unavailable");
  await expect(context.getByText("Uses Lambda")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Message Pingo" })).toBeEnabled();
});
