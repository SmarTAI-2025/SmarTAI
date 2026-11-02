import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const permutation = String.raw`\begin{array}{r@{\quad}r@{\quad}r@{\quad}r@{\quad}r}
1 \mapsto 13 & 2 \mapsto 2 & 3 \mapsto 15 & 4 \mapsto 14 & 5 \mapsto 10 \\
6 \mapsto 6 & 7 \mapsto 12 & 8 \mapsto 3 & 9 \mapsto 4 & 10 \mapsto 1 \\
11 \mapsto 7 & 12 \mapsto 9 & 13 \mapsto 5 & 14 \mapsto 11 & 15 \mapsto 8
\end{array}`;

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
      stem: `Let $\\sigma$ be the permutation\n$$\n${permutation}\n$$\nFind its disjoint cycles.`,
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
      if (path === "/tasks/math-display/problems/q1" && request.method() === "PUT") {
        const patch = request.postDataJSON() as { stem: string };
        expect(patch.stem).toContain(permutation);
        expect(patch.stem).toContain(`$$\n${permutation}\n$$`);
        problem.stem = patch.stem;
        task.workflow_revision += 1;
        return json({ status: "ok", q_id: "q1", problem, workflow_revision: task.workflow_revision });
      }
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
    expect(mutations).toEqual([]);
    await expect(card.locator("mtable mtr")).toHaveCount(3);
    const originalStem = problem.stem;
    await card.getByRole("button", { name: "题目 · 修改", exact: true }).click();
    const editor = card.getByRole("textbox", { name: "题目", exact: true });
    await expect(editor).toHaveValue(originalStem);
    await editor.fill(originalStem + "\nTeacher note.");
    await card.getByRole("button", { name: "保存", exact: true }).click();
    await expect(editor).toHaveCount(0);
    await page.reload();
    await expect(card.locator("mtable mtr")).toHaveCount(3);
    await expect(card.locator(".katex-error")).toHaveCount(1); // Only the deliberately broken fraction remains.
    await card.getByRole("button", { name: "题目 · 修改", exact: true }).click();
    await expect(editor).toHaveValue(originalStem + "\nTeacher note.");
    await card.getByRole("button", { name: "题目 · 取消修改", exact: true }).click();
    await page.screenshot({ path: info.outputPath("math-review.png"), fullPage: true });
    const stemSection = card.locator("section").filter({ has: page.getByRole("heading", { name: "题目", exact: true }) });
    await stemSection.screenshot({ path: info.outputPath("array-rendered.png") });
    if (width === 390) {
      const formula = stemSection.locator(".katex-display");
      const scroll = await formula.evaluate(element => {
        element.scrollLeft = element.scrollWidth;
        return { left: element.scrollLeft, max: element.scrollWidth - element.clientWidth };
      });
      expect(scroll.max).toBeGreaterThan(0);
      expect(scroll.left).toBeCloseTo(scroll.max, 0);
      await stemSection.screenshot({ path: info.outputPath("array-scrolled.png") });
    }
    expect(mutations).toEqual(["/tasks/math-display/problems/q1"]);
    expect(errors).toEqual([]);
  });
}
