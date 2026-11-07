import { expect, test } from "@playwright/test";

const headings = ["任务", "课程 / 标签", "当前阶段", "进度", "预计剩余", "最近更新", "下一步", "操作"];
const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const width of [390, 1024, 1280, 1440, 1920]) {
  test(`teacher searches tasks, confirms deletion and opens the same guide at ${width}px`, async ({ page }, testInfo) => {
    const errors: string[] = [], deletes: string[] = [], asks: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-teacher");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    const course = { id: "cs", name: "计算机基础", code: "CS101" };
    const tag = { id: "mid", name: "期中", color: "teal" };
    const task = { task_id: "practice", name: "数据结构练习", semester_id: "2026-2027-autumn", owner_id: "teacher",
      course_id: course.id, tag_ids: [tag.id], workflow_revision: 1, status: "graded", problem_count: 2,
      student_count: 1, kb_docs: {}, kb_doc_count: 0, created_at: 1, updated_at: 2, progress_percent: 100, eta_seconds: 0 };
    const other = { ...task, task_id: "vectors", name: "向量练习", course_id: null, tag_ids: [] as string[] };
    let tasks = [other, task];
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "teacher", username: "示例教师", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher" });
      if (path === "/knowledge/activity") return json({ items: [], total: 0, active_count: 0 });
      if (path === "/courses/") return json([course]);
      if (path === "/tags/") return json([tag]);
      if (path === "/tasks/") {
        if (!url.searchParams.has("page")) return json(Object.fromEntries(tasks.map(t => [t.task_id, t])));
        const items = tasks.filter(t => t.name.includes(url.searchParams.get("q") ?? "") &&
          (!url.searchParams.has("course_id") || t.course_id === url.searchParams.get("course_id")) &&
          (!url.searchParams.has("tag_ids") || t.tag_ids.includes(url.searchParams.get("tag_ids")!)));
        return json({ items, total: items.length, available_facets: { semesters: [], statuses: {}, courses: [course], tags: [tag] } });
      }
      if (path === "/analytics/ask") {
        const body = request.postDataJSON();
        expect(body.surface).toBe("history");
        asks.push(body.question);
        const match = tasks.find(t => t.task_id === task.task_id) ?? other;
        return json({ recognized: true, kind: "tasks", explanation: "", data: { columns: ["task_id"], rows: [[match.task_id]] },
          selection: { kind: "tasks", ids: [match.task_id] }, tasks: [match], chart: null });
      }
      if (request.method() === "DELETE" && tasks.some(t => path === `/tasks/${t.task_id}`)) {
        deletes.push(path);
        tasks = tasks.filter(t => path !== `/tasks/${t.task_id}`);
        return json({ status: "deleted" });
      }
      return json([]);
    });

    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: "工作台", exact: true })).toBeVisible();
    await expect(page.locator('vite-error-overlay')).toHaveCount(0);
    const search = page.getByRole("textbox", { name: "SmarTAI 智能筛选任务" });
    const run = async (text: string) => { await search.fill(text); await search.press("Enter"); };
    await expect(page.locator('[data-ask-smartai] [data-smartai-mascot]')).toHaveCount(0);
    await expect(page.getByText("我可以按学期找作业，也能帮你找出待复核的批改。")).toBeVisible();
    if (width >= 1280) {
      const table = page.getByRole("table", { name: "需要关注的任务" });
      const attentionRows = table.getByRole("rowgroup");
      await expect(table.getByRole("columnheader")).toHaveText(headings);
      await expect(table).toHaveCSS("border-top-width", "1px");
      for (const corner of ["top-left", "top-right", "bottom-right", "bottom-left"]) {
        await expect(table).toHaveCSS(`border-${corner}-radius`, "8px");
      }
      const row = attentionRows.getByRole("row").filter({ hasText: task.name });
      await expect(row.getByRole("cell").nth(1)).toHaveText("计算机基础 · 期中");
      const stage = await row.getByText("批改完成").boundingBox();
      const stageHeader = await table.getByRole("columnheader", { name: "当前阶段" }).boundingBox();
      expect(Math.abs(stage!.x + stage!.width / 2 - stageHeader!.x - stageHeader!.width / 2)).toBeLessThanOrEqual(1);
      expect(stage!.width).toBeLessThan(100);

      const metrics = page.getByRole("region", { name: "工作台概览" });
      const drafts = page.getByRole("region", { name: "暂存草稿" });
      const bounds = await table.boundingBox();
      for (const section of [metrics, drafts]) {
        const reference = await section.boundingBox();
        expect(Math.abs(bounds!.x - reference!.x)).toBeLessThanOrEqual(1);
        expect(Math.abs(bounds!.x + bounds!.width - reference!.x - reference!.width)).toBeLessThanOrEqual(1);
      }
      const head = page.getByRole("columnheader", { name: "下一步" });
      const cell = attentionRows.getByRole("row").first().getByRole("cell").nth(6);
      const [header, action] = await Promise.all([head.boundingBox(), cell.boundingBox()]);
      expect(Math.abs(header!.x - action!.x)).toBeLessThanOrEqual(1);
      console.log(`Dashboard at ${width}px: left=${bounds!.x}, right=${bounds!.x + bounds!.width}, width=${bounds!.width}`);
    }
    await page.screenshot({ path: testInfo.outputPath("dashboard.png") });
    await run("哪些批改需要复核？");
    const results = page.getByRole("table", { name: "历史任务列表" });
    await expect(results.getByRole("link", { name: task.name, exact: true })).toBeVisible();
    expect(asks).toEqual(["哪些批改需要复核？"]);

    await page.goto("/history");
    await expect(page.getByRole("textbox")).toHaveCount(1);
    if (width >= 768) await expect(results.getByRole("columnheader")).toHaveText(headings);
    for (const [text, param, value] of [["结构", "q", "结构"], ["CS101", "course", "cs"], ["期中", "tags", "mid"]]) {
      await run(text);
      await expect.poll(() => new URL(page.url()).searchParams.get(param)).toBe(value);
      await expect(results.getByRole("link", { name: task.name, exact: true })).toBeVisible();
      await expect(results.getByRole("link", { name: other.name, exact: true })).toHaveCount(0);
    }
    expect(asks).toHaveLength(1);
    await page.getByRole("button", { name: "清空查询" }).click();
    await expect(results.getByRole("link", { name: other.name, exact: true })).toBeVisible();
    const open = results.getByRole("link", { name: `打开任务 · ${task.name}` });
    const remove = results.getByRole("button", { name: `删除任务 · ${task.name}` });
    await expect(open).toHaveAttribute("href", /\/tasks\/practice\/review$/);
    await expect(remove).toBeInViewport({ ratio: 1 });
    const actions = results.getByRole("columnheader", { name: "操作", exact: true });
    await expect(actions).toBeVisible();
    const [heading, eye, bin] = await Promise.all([actions.boundingBox(), open.boundingBox(), remove.boundingBox()]);
    expect(Math.abs((eye!.x + bin!.x + bin!.width) / 2 - (heading!.x + heading!.width / 2))).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath("history.png") });
    await remove.click();
    const dialog = page.getByRole("alertdialog", { name: "删除任务" });
    await expect(dialog).toContainText(task.name);
    await expect(dialog.getByRole("button", { name: "取消" })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath("confirm.png") });
    await dialog.getByRole("button", { name: "取消" }).click();
    expect(deletes).toEqual([]);
    await expect(remove).toBeFocused();
    await remove.click();
    await dialog.getByRole("button", { name: "确认删除" }).click();
    await expect(remove).toHaveCount(0);
    expect(deletes).toEqual(["/tasks/practice"]);
    // Deleting from the attention list must also retire the same row from a prior Ask selection.
    await page.goto("/dashboard");
    await run("哪些批改需要复核？");
    await expect(results.getByRole("link", { name: other.name, exact: true })).toBeVisible();
    const attention = page.getByRole("region", { name: "需要关注的任务" });
    await attention.getByRole("button", { name: `删除任务 · ${other.name}` }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "确认删除" }).click();
    await expect(attention.getByText(other.name, { exact: true })).toHaveCount(0);
    await expect(results.getByRole("link", { name: other.name, exact: true })).toHaveCount(0);
    expect(deletes).toEqual(["/tasks/practice", "/tasks/vectors"]);
    if (width < 1024) await page.getByRole("button", { name: "打开导航" }).click();
    const guide = page.getByRole("navigation").getByRole("link", { name: "使用指南" });
    await expect(guide).toHaveAttribute("href", "/guide/?lang=zh");
    await guide.click();
    await expect(page.locator("#quickstart")).toBeVisible();
    expect(errors).toEqual([]);
  });
}
