import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const fixture = "/e2e/fixtures/pdf-preview.html";
const input = (page: Page) => page.getByRole("spinbutton", { name: "PDF page" });
const scroll = (page: Page) => page.getByTestId("pdf-scroll-container");
const canvas = (page: Page, number: number) => page.getByLabel(`PDF page ${number}`, { exact: true });
async function ready(page: Page, number: number) {
  await expect(canvas(page, number)).toHaveAttribute("data-rendered", "true");
}
async function jump(page: Page, number: string) {
  await input(page).fill(number);
  await input(page).press("Enter");
}
async function choose(page: Page, file: string) {
  await page.getByRole("button", { name: file, exact: true }).click();
  await ready(page, 1);
}

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(fixture);
  await ready(page, 1);
});

test("wheel reads all 15 pages, syncs the counter and keeps the question selection", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await expect(page).toHaveTitle("PDF preview local acceptance");
  await expect(page.getByRole("heading", { name: "题目审核 · PDF 本地隔离验收" })).toBeVisible();
  await page.getByRole("button", { name: "定位题目 2（第 8 页）" }).click();
  await expect(input(page)).toHaveValue("8");
  await ready(page, 8);
  await jump(page, "1");
  await scroll(page).hover();
  const visited = new Set<number>([1]);
  for (let i = 0; i < 80 && !visited.has(15); i++) {
    await page.mouse.wheel(0, 400);
    await page.waitForTimeout(45);
    const current = Number(await input(page).inputValue());
    visited.add(current);
    await ready(page, current);
    expect(await page.locator("canvas").count()).toBeLessThanOrEqual(5);
    if (current === 5) await page.screenshot({ path: test.info().outputPath("beyond-old-stop.png") });
  }
  expect([...visited].sort((a, b) => a - b)).toEqual(Array.from({ length: 15 }, (_, i) => i + 1));
  await expect(page.getByRole("heading", { name: "题目 2 · 教师复核" })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("page-15.png") });
  await page.mouse.wheel(0, -1000);
  await expect.poll(async () => Number(await input(page).inputValue())).toBeLessThan(15);
  await page.getByTestId("question-content").hover();
  await page.mouse.wheel(0, 600);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
  expect(errors).toEqual([]);
});

test("input, adjacent arrows, invalid input, single-page boundaries and local failure", async ({ page }) => {
  await jump(page, "12");
  await ready(page, 12);
  await page.getByRole("button", { name: "Next pages" }).click();
  await expect(input(page)).toHaveValue("13");
  await page.getByRole("button", { name: "Previous pages" }).click();
  await expect(input(page)).toHaveValue("12");
  for (const [typed, expected] of [["900", "15"], ["0", "1"], ["-3", "1"], ["4.9", "4"], ["", "4"]]) {
    await jump(page, typed);
    await expect(input(page)).toHaveValue(expected);
  }
  await choose(page, "single");
  await expect(page.getByRole("button", { name: "Next pages" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Previous pages" })).toBeDisabled();
  await page.getByRole("button", { name: "broken", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("load-failure.png") });
  await choose(page, "multiple");
});

test("long PDF stays virtual, handles jumps and splitter/viewport/browser zoom", async ({ page }) => {
  await choose(page, "long");
  await jump(page, "200");
  await ready(page, 200);
  await scroll(page).hover();
  for (let i = 0; i < 8; i++) await page.mouse.wheel(0, 700);
  await expect.poll(async () => Number(await input(page).inputValue())).toBeGreaterThan(204);
  await ready(page, Number(await input(page).inputValue()));
  for (const number of [15, 239, 80, 240]) {
    await jump(page, String(number));
    await ready(page, number);
    expect(await page.locator("canvas").count()).toBeLessThanOrEqual(5);
    const pixels = await page.locator("canvas").evaluateAll((nodes) => nodes.reduce((total, node) => total + (node as HTMLCanvasElement).width * (node as HTMLCanvasElement).height, 0));
    expect(pixels).toBeLessThanOrEqual(16_000_000);
  }
  await expect(page.getByRole("button", { name: "Next pages" })).toBeDisabled();
  await page.screenshot({ path: test.info().outputPath("page-240.png") });
  const separator = page.getByRole("separator");
  await separator.focus();
  await separator.press("ArrowLeft");
  await separator.press("ArrowLeft");
  await ready(page, 240);
  await expect(input(page)).toHaveValue("240");
  await page.setViewportSize({ width: 1200, height: 850 });
  await ready(page, 240);
  await expect(input(page)).toHaveValue("240");
  await page.evaluate(() => { document.documentElement.style.zoom = "1.25"; });
  await ready(page, 240);
  await expect(input(page)).toHaveValue("240");
  await choose(page, "mixed");
  for (const number of [18, 19, 20, 6]) {
    await jump(page, String(number));
    await ready(page, number);
    await expect(input(page)).toHaveValue(String(number));
  }
});

test("fast file switch ignores delayed source and unmount terminates the worker", async ({ page }) => {
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/slow.pdf", async (route) => {
    await delayed;
    await route.fulfill({ path: new URL("./fixtures/long.pdf", import.meta.url).pathname, contentType: "application/pdf" }).catch(() => undefined);
  });
  await page.getByRole("button", { name: "slow", exact: true }).click();
  await choose(page, "single");
  release();
  await page.waitForTimeout(200);
  await expect(input(page)).toHaveAttribute("max", "1");
  for (const file of ["long", "multiple", "mixed", "single"]) await choose(page, file);
  await expect.poll(() => page.workers().length).toBe(1);
  await page.getByRole("button", { name: "Toggle preview" }).click();
  await expect(page.locator("canvas")).toHaveCount(0);
  await expect.poll(() => page.workers().length).toBe(0);
});

test("mobile preview and outer scroll remain usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await ready(page, 1);
  await jump(page, "8");
  await ready(page, 8);
  const bounds = await scroll(page).boundingBox();
  expect(bounds!.height).toBeGreaterThan(150);
  expect(bounds!.width).toBeLessThan(390);
  await scroll(page).hover();
  await page.mouse.wheel(0, 600);
  await expect.poll(async () => Number(await input(page).inputValue())).toBeGreaterThan(8);
  await page.screenshot({ path: test.info().outputPath("mobile-scroll.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByTestId("question-content").hover();
  await page.mouse.wheel(0, 500);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
});


test("real question-review route retains question navigation while the original PDF scrolls", async ({ page }) => {
  const pdf = readFileSync(new URL("./fixtures/multiple.pdf", import.meta.url));
  const unexpected: string[] = [];
  const errors: string[] = [];
  const fontWarnings: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    // The synthetic china-s font intentionally has no embedded glyphs, exercising
    // the existing CMap/font fallback. Preserve its known warnings as evidence.
    if (message.type() === "warning" && [
      "Warning: Cannot load system font: Heiti, installing it could help to improve PDF rendering.",
      'Warning: Required "glyf" table is not found -- trying to recover.',
    ].includes(message.text())) fontWarnings.push(message.text());
    else if (["warning", "error"].includes(message.type())) errors.push(message.text());
  });
  const task = {
    task_id: "local-pdf", name: "本地 PDF 复核验收", status: "problems_ready", workflow_revision: 1,
    problem_file_name: "multiple.pdf", problem_data: Object.fromEntries([1, 2].map((n) => [`Q${n}`, {
      q_id: `Q${n}`, number: String(n), type: "Proof", stem: String.raw`合成题目 ${n}：证明 $a^2 + b^2 \geq 2ab$。`,
      max_score: 10, criterion: "论证完整 10 分", reference_answer: "由平方非负可得。", preparation_issues: [],
    }])), students: [], created_at: 0,
  };
  await page.route("http://localhost:8000/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const cors = { "access-control-allow-origin": new URL(page.url()).origin, "access-control-allow-credentials": "true", "access-control-allow-headers": "*" };
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (route.request().method() !== "GET") { unexpected.push(path); return route.abort(); }
    if (path.endsWith("/content")) return route.fulfill({ body: pdf, contentType: "application/pdf", headers: cors });
    const value = path === "/auth/me" ? { id: "synthetic", username: "PDF local test", email: "local@example.invalid", role: "teacher", is_active: true, created_at: 0 }
      : path === "/experts/available" ? []
      : path === "/tasks/local-pdf" ? task
      : path === "/tasks/local-pdf/source-files" ? { task_id: "local-pdf", workflow_revision: 1, problem_source: { source_id: "pdf", file_id: "pdf", display_name: "multiple.pdf", mime_type: "application/pdf", size_bytes: pdf.length, status: "available", preview_kind: "pdf" }, submission_sources: {} }
      : null;
    if (value === null) unexpected.push(path);
    await route.fulfill({ json: value ?? {}, headers: cors });
  });
  await page.evaluate(() => localStorage.setItem("smartai_token", "synthetic-local-pdf-only"));
  await page.goto("/tasks/local-pdf/questions/Q1/content");
  await expect(page.getByRole("heading", { name: "题目资料审核", exact: true })).toBeVisible().catch((error) => { throw new Error(`${error}\n${JSON.stringify({ errors, unexpected })}`); });
  await page.getByRole("button", { name: "查看题目原文件" }).click();
  await ready(page, 1);
  await scroll(page).hover();
  for (let i = 0; i < 15; i++) await page.mouse.wheel(0, 350);
  await expect.poll(async () => Number(await input(page).inputValue())).toBeGreaterThan(4);
  await ready(page, Number(await input(page).inputValue()));
  await expect(page).toHaveURL(/questions\/Q1\/content/);
  await page.screenshot({ path: test.info().outputPath("actual-question-review.png") });
  const readingPage = await input(page).inputValue();
  const outerBefore = await page.evaluate(() => window.scrollY);
  await scroll(page).focus();
  await scroll(page).press("ArrowDown");
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(outerBefore);
  await page.getByRole("button", { name: "下一题", exact: true }).first().click();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(outerBefore);
  await expect(input(page)).toHaveValue(readingPage);
  await test.info().attach("synthetic-cmap-font-warnings", { body: fontWarnings.join("\n"), contentType: "text/plain" });
  expect(unexpected).toEqual([]);
  expect(errors).toEqual([]);
});
