import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const width of [1440, 390]) {
  test(`saved math remains readable and nonblocking in question review (${width}px)`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [], mutations: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-math-token");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    const problem = { q_id: "q1", number: "1.1.7", type: "reasoning", max_score: 10,
      stem: String.raw`Prove that \(G\) is an abelian group.`,
      reference_answer: String.raw`**结合律：**
$$ (x * y) * z = x + y + z - [x + y + z]$（等于 $x * (y * z)$）。

**单位元：** \(0 * x = x\)。

**逆元：** \[x+y=1\]

保留待编辑的公式：$\frac{1}{$。

后续证明仍然可读。`,
      criterion: "按证明中的数学依据给分。\n- 正确证明结合律、单位元、逆元与交换律：10分",
      review_status: "confirmed", max_score_review_status: "confirmed", preparation_issues: [] };
    const task = { task_id: "math-display", name: "Math display", status: "problems_ready", workflow_revision: 1,
      problem_data: { q1: problem }, student_data: {}, problem_count: 1, student_count: 0,
      created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-math-token" });
      if (request.method() !== "GET") mutations.push(path);
      if (path === "/tasks/math-display") return json(task);
      if (path.endsWith("/source-files")) return json({ problem_source: null, submission_sources: {} });
      return json([]);
    });
    await page.goto("/tasks/math-display/questions");
    await expect(page).toHaveTitle("SmarTAI");
    await expect(page.getByRole("tab", { name: "需要处理 (0)" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "建议处理 (0)" })).toBeVisible();
    await page.locator("table tbody tr").getByRole("link", { name: "查看", exact: true }).click();
    await expect(page).toHaveURL(/\/questions\/q1\/content/);
    const card = page.locator('[data-question-id="q1"]');
    await expect(card.locator("strong").filter({ hasText: "单位元：" })).toBeVisible();
    await expect(card.getByText("后续证明仍然可读。")).toBeVisible();
    await expect(card.locator(".katex-error")).toHaveCount(1);
    await expect(card.locator(".katex-error")).toHaveCSS("color", await card.evaluate(el => getComputedStyle(el).color));
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: info.outputPath("math-review.png"), fullPage: true });
    expect(mutations).toEqual([]);
    expect(errors).toEqual([]);
  });
}
