import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const taskId = "asg_shared_ask";
// Optional live bridge uses the same synthetic records with the actual configured model.
const liveBridge = process.env.SMARTAI_ASK_LIVE_BRIDGE;
const names = ["Alex Example", "Maya Example", "Jordan Example", "Taylor Example"];
const problem_data = Object.fromEntries([1, 2, 3, 4].map(n => [`q${n}`, { q_id: `q${n}`, number: String(n), stem: `Synthetic question ${n}`, max_score: 10, type: "short_answer" }]));
const student_data = Object.fromEntries(names.map((name, i) => [`S${i}`, { stu_id: `S${i}`, stu_name: name, identity_status: "matched", stu_ans: Object.keys(problem_data).map(q_id => ({ q_id, content: "Synthetic answer", review_status: "confirmed", flag: [] })) }]));
const results = names.map((name, i) => ({ student_id: `S${i}`, student_name: name, corrections: Object.keys(problem_data).map(q_id => ({ q_id, score: [9, 6, 5, null][i], provisional_score: [9, 6, 5, null][i], max_score: 10, confidence: .9, review_status: "confirmed", comment: "Synthetic feedback" })) }));
const task = { task_id: taskId, owner_id: "shared-owner", name: "Shared Ask fixture", status: "finalized", workflow_revision: 1, final_result_version: 1, problem_data, student_data, problem_count: 4, student_count: 4, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
const evidence = { explanation: "Internal SQL reasoning", sql: "SELECT private_evidence FROM students", assumptions: ["Internal assumptions"], fingerprint: "internal-snapshot", candidates: [] };

// Production router/shared UI. With the optional bridge, Ask uses real model responses;
// authentication and task records remain synthetic in both guest and teacher sessions.
for (const demo of [true, false]) for (const locale of ["zh-CN", "en-US"]) {
  test(`${demo ? "Demo" : "Teacher"} ${locale}: main-table selection, reset, and charts`, async ({ page }, testInfo) => {
    test.setTimeout(liveBridge ? 240_000 : 30_000);
    const errors: string[] = [], asks: { question: string; history?: string[] }[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(({ demo, locale, taskId }) => {
      localStorage.setItem("smartai_locale", locale);
      if (demo) {
        sessionStorage.setItem("smartai_frontier_token", "synthetic-guest-token");
        sessionStorage.setItem("smartai_frontier_owner", "shared-owner");
        sessionStorage.setItem("smartai_frontier_generation", "1");
        sessionStorage.setItem("smartai_frontier_tasks", JSON.stringify([taskId]));
      } else localStorage.setItem("smartai_token", "synthetic-teacher-token");
    }, { demo, locale, taskId });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "shared-owner", username: "Synthetic teacher", role: "teacher", is_active: true, auth_scope: demo ? "frontier_demo" : "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (path.startsWith("/experts") || path === "/tags/") return json([]);
      if (path === `/tasks/${taskId}`) return json(task);
      if (path.endsWith("/result")) return json({ task_id: taskId, problem_data, student_data, results, timestamp: 1 });
      if (path.endsWith("/finalization")) return json({ task_id: taskId, workflow_revision: 1, final_result_version: 1, final_result_dirty: false, analysis_status: "ready", remaining_review_count: 0 });
      if (path.endsWith("/input")) return json({ task_id: taskId, workflow_revision: 1, input: null });
      if (liveBridge && path.startsWith(`/analytics/${taskId}/`)) {
        const body = request.postDataJSON(); asks.push(body);
        const response = await page.request.post(`${liveBridge}${path}`, { data: body, timeout: 180_000 });
        return route.fulfill({ response });
      }
      if (path === `/analytics/${taskId}/ask`) {
        const body = request.postDataJSON(); asks.push(body);
        const ids = body.question.includes("Zed Absent") ? [] : body.question === "Alex" ? ["S0"] : ["S0", "S1", "S2", "S3"];
        return json({ ...evidence, recognized: true, kind: "students", data: { columns: ["student_id"], rows: ids.map(id => [id]) }, selection: { kind: "students", ids }, chart: null });
      }
      if (path === `/analytics/${taskId}/query`) {
        const body = request.postDataJSON(); asks.push(body);
        if (/散点图|scatterplot/.test(body.question)) {
          const chart = { mode: "chart", title: "查询结果 / Query result", traces: [{ type: "scatter", name: "avg_confidence", x: [90, 60, 50], y: [.9, .9, .9] }] };
          return json({ ...chart, execution: { ...evidence, recognized: true, kind: "chart", data: { columns: [], rows: [] }, chart, selection: null } });
        }
        if (body.question === "显示及格与未及格人数" || body.question === "Show pass and fail counts") {
          const chart = { mode: "chart", title: "查询结果 / Query result", traces: [{ type: "bar", name: "student_count", x: ["Passed", "Failed", "Unscored"], y: [2, 1, 1] }] };
          return json({ ...chart, execution: { ...evidence, recognized: true, kind: "chart", data: { columns: ["status", "student_count"], rows: [["Passed", 2], ["Failed", 1], ["Unscored", 1]] }, chart, selection: null } });
        }
        const chart = { mode: "chart", title: "查询结果 / Query result", rationale: "Internal chart explanation", traces: [{ type: "bar", name: "avg_percent", x: ["Q1", "Q2", "Q3", "Q4"], y: [200/3, 200/3, 200/3, 200/3] }, { type: "bar", name: "low_confidence_count", x: ["Q1", "Q2", "Q3", "Q4"], y: [0, 0, 0, 0] }] };
        return json({ ...chart, execution: { ...evidence, recognized: true, kind: "chart", data: { columns: ["number"], rows: [["Q1"]] }, chart, selection: null } });
      }
      throw new Error(`Unexpected API request ${request.method()} ${path}`);
    });
    const zh = locale === "zh-CN";
    await page.goto(`/tasks/${taskId}/results/students`);
    const table = page.getByRole("table"), input = page.getByRole("textbox", { name: zh ? "Ask SmarTAI：学生分析" : "Ask SmarTAI: student analysis" });
    const rows = () => table.locator("tbody tr");
    const clear = () => page.getByRole("button", { name: zh ? "清空查询" : "Clear query" }).click();
    const submit = () => page.getByRole("button", { name: "Ask SmarTAI", exact: true }).click();
    await expect(rows()).toHaveCount(4);
    await input.fill("Alex"); await submit();
    await expect(rows()).toHaveCount(1, { timeout: liveBridge ? 180_000 : 5000 }); await expect(rows().first()).toContainText("Alex Example");
    await expect(page.getByRole("table")).toHaveCount(1);
    await expect(page.getByText(/Internal|private_evidence|internal-snapshot/)).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("alex-only.png"), fullPage: true });
    await clear(); await expect(rows()).toHaveCount(4);
    await input.fill(zh ? "请按得分率由高到低排列所有学生" : "Sort students by score percentage, highest first"); await submit();
    await expect(rows().nth(1)).toContainText("Maya Example", { timeout: liveBridge ? 180_000 : 5000 });
    await page.getByRole("columnheader").first().getByRole("button").click();
    await clear();
    await expect(rows().nth(0)).toContainText("Alex Example");
    await expect(rows().nth(1)).toContainText("Jordan Example");
    await expect(page).not.toHaveURL(/column_sort|sort=|ask_order/);
    await input.fill(zh ? "查找姓名为 Zed Absent 的学生" : "Find the student named Zed Absent"); await submit();
    await expect(table.getByText("Alex Example", { exact: true })).toHaveCount(0, { timeout: liveBridge ? 180_000 : 5000 });
    expect(asks.at(-1)?.history ?? []).toEqual([]);
    await clear(); await expect(rows()).toHaveCount(4);
    await page.goto(`/tasks/${taskId}/results/visualizations`);
    await page.getByRole("button", { name: zh ? "比较各题得分率与低置信题次" : "Compare question score percentages and low-confidence counts", exact: true }).click();
    await submit();
    await expect(page.getByRole("heading", { name: zh ? "自定义图表" : "Custom chart", exact: true })).toBeVisible({ timeout: liveBridge ? 180_000 : 5000 });
    await expect(page.getByText(/Internal|private_evidence|internal-snapshot/)).toHaveCount(0);
    const preview = page.locator('[data-chart-export="generated-preview"]');
    await expect(preview.locator('.recharts-wrapper > svg')).toHaveCount(2);
    await expect(preview).toContainText(zh ? "低置信题次" : "Low-confidence count");
    await page.screenshot({ path: testInfo.outputPath("charts.png"), fullPage: true });
    await clear();
    await expect(page.getByRole("heading", { name: zh ? "自定义图表" : "Custom chart", exact: true })).toHaveCount(0);
    await page.getByRole("textbox", { name: zh ? "Ask SmarTAI：图表分析" : "Ask SmarTAI: chart analysis" }).fill(zh ? "显示及格与未及格人数" : "Show pass and fail counts");
    const counted = page.waitForResponse(r => r.url().endsWith(`/analytics/${taskId}/query`), { timeout: liveBridge ? 180_000 : 5000 });
    await submit();
    const response = await (await counted).json();
    expect(response.mode).toBe("chart");
    const counts: Record<string, number> = {};
    for (const trace of response.traces) (trace.y ?? trace.values).forEach((value: number, i: number) => {
      const label = String((trace.x ?? trace.labels)[i]).toLowerCase();
      if (/fail|未及格|不及格/.test(label)) counts.fail = value;
      else if (/pass|及格/.test(label)) counts.pass = value;
      else if (/unscored|未评分/.test(label)) counts.unscored = value;
    });
    expect(counts).toEqual({ pass: 2, fail: 1, unscored: 1 }); // Students, not twelve scored question records; 60% passes.
    await expect(preview.locator('.recharts-wrapper > svg').first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("pass-counts.png"), fullPage: true });
    await clear();
    await expect(page.locator('[data-chart-export="generated-preview"]')).toHaveCount(0);
    await page.getByRole("textbox", { name: zh ? "Ask SmarTAI：图表分析" : "Ask SmarTAI: chart analysis" }).fill(zh
      ? "画出散点图：横轴为学生得分率，纵轴为平均置信度，并区分需要复核的学生"
      : "Plot a scatterplot: x axis student score percentage, y axis average confidence, grouped by review status");
    const plotted = page.waitForResponse(r => r.url().endsWith(`/analytics/${taskId}/query`), { timeout: liveBridge ? 180_000 : 5000 });
    await submit();
    const scatter = await (await plotted).json();
    expect(scatter.mode).toBe("chart");
    const points: number[][] = scatter.traces.flatMap((trace: { type: string; x: (number | null)[]; y: (number | null)[] }) => {
      expect(trace.type).toBe("scatter");
      return trace.x.map((x, i) => [x, trace.y[i]]).filter(point => point.every(value => typeof value === "number"));
    });
    expect(points.sort((a, b) => a[0] - b[0])).toEqual([[50, .9], [60, .9], [90, .9]]);
    await expect(preview.locator('.recharts-wrapper > svg').first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("scatter.png"), fullPage: true });
    await clear();
    await expect(page.locator('[data-chart-export="generated-preview"]')).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}
