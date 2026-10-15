import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const mode of ["individual", "overview-all", "detail-all"]) for (const locale of ["zh-CN", "en-US"]) {
  test(`${mode} ${locale}: confirmation, live results and queue stay consistent`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN", taskId = "asg_review_status";
    const errors: string[] = [];
    const writes: string[] = [];
    let reportGenerationRequested = false, reportsGenerated = false;
    let reportDirty = false;
    let reportVersions: number[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.addInitScript(locale => {
      localStorage.setItem("smartai_locale", locale);
      localStorage.setItem("smartai_token", "synthetic-teacher-token");
    }, locale);
    const problem_data = Object.fromEntries([1, 2, 3, 4].map(n => [`q${n}`, { q_id: `q${n}`, number: String(n), stem: `Synthetic question ${n}.`, criterion: "Reasoning rubric.", max_score: 10 }]));
    const student_data = { S1: { stu_id: "S1", stu_name: "Example", identity_status: "matched", stu_ans: [1, 2, 3, 4].map(n => ({ q_id: `q${n}`, content: "Synthetic answer.", review_status: "confirmed", flag: [] })) } };
    const corrections = [6, 8, 4, 10].map((score, i) => ({ q_id: `q${i + 1}`, score, provisional_score: score, max_score: 10,
      confidence: i === 1 ? .5 : .95, comment: "Original explanation.", steps: [], requires_human_review: i === 0 || i === 2,
      review_reasons: i === 0 || i === 2 ? ["high_indecisiveness"] : [], initial_review_reasons: [] as string[],
      review_status: "pending", teacher_score: null as number | null, teacher_comment: "",
      expert_results: (i === 0 ? [3, 9] : i === 2 ? [0, 8] : [score, score]).map((value, index) => ({ provider: `expert-${index}`, score: i === 1 && index === 1 ? 0 : value,
        max_score: 10, confidence: .95, comment: "Evidence.", steps: [], error_kind: i === 1 && index === 1 ? "general" : null })) }));
    const task = { task_id: taskId, owner_id: "teacher", name: "Review consistency", status: "graded", workflow_revision: 1,
      final_result_version: 0, problem_data, student_data, problem_count: 4, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {} };
    const finalization = () => ({ task_id: taskId, task_status: task.status, workflow_revision: task.workflow_revision, final_result_version: task.final_result_version,
      final_result_dirty: reportDirty, analysis_status: reportDirty ? "stale" : reportsGenerated ? "ready" : "not_generated", remaining_review_count: 0, required_review_count: 0,
      confirmed_required_count: 0, remaining_reviews: [], ready_for_confirmation: true });
    const artifacts = () => ({ task_id: taskId, current_result_version: task.final_result_version, analysis_status: reportsGenerated ? "ready" : "not_generated",
      versions: reportVersions.map(version => ({ version, current: version === task.final_result_version,
        status: version !== task.final_result_version ? "historical" : reportDirty ? "stale" : "ready", generated_at: 1_700_000_000 + version,
        bundle_filename: `${task.name}_2023-11-14_v${version}_reports.zip`,
        files: [{ artifact_id: "grades_csv", title: "成绩 CSV", title_en: "Grades CSV", filename: `${task.name}_2023-11-14_v${version}_grades.csv`, size_bytes: 12 }] })) });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "teacher", username: "Synthetic teacher", role: "teacher", is_active: true, auth_scope: "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (request.method() !== "GET") {
        writes.push(path);
        if (path.endsWith("/artifacts/generate")) {
          expect(reportGenerationRequested).toBe(true);
          expect(request.postDataJSON().expected_workflow_revision).toBe(task.workflow_revision);
          task.workflow_revision += 1; task.final_result_version += 1; task.status = "review_confirmed";
          reportDirty = false; reportsGenerated = true;
          reportVersions.unshift(task.final_result_version);
          return json({ ...finalization(), status: "ok", unchanged: false, artifacts: artifacts() });
        }
        if (request.method() === "DELETE" && path.endsWith("/artifacts/1")) {
          reportVersions = reportVersions.filter(version => version !== 1);
          return json(artifacts());
        }
        expect(path).toMatch(/\/reviews\/S1\/q[1-4]$/);
        const input = request.postDataJSON();
        expect(input.expected_workflow_revision).toBe(task.workflow_revision);
        expect(input.confirm).toBe(true);
        const correction = corrections.find(c => path.endsWith(`/${c.q_id}`))!;
        const unchanged = correction.score === input.teacher_score;
        Object.assign(correction, { teacher_score: input.teacher_score, score: input.teacher_score, teacher_comment: input.teacher_comment,
          review_status: "confirmed", requires_human_review: false, initial_review_reasons: correction.review_reasons, review_reasons: [] });
        task.workflow_revision += 1;
        return json({ status: "ok", unchanged, student_id: "S1", q_id: correction.q_id, correction, workflow_revision: task.workflow_revision });
      }
      if (path.startsWith("/experts") || path === "/tags/") return json([]);
      if (path === `/tasks/${taskId}`) return json(task);
      if (path.endsWith("/result")) return json({ task_id: taskId, problem_data, student_data, results: [{ student_id: "S1", student_name: "Example", corrections }], timestamp: 1 });
      if (path.endsWith("/teacher_comments")) return json({ comments: {} });
      if (path.endsWith("/finalization")) return json(finalization());
      if (path.endsWith("/artifacts")) return json(artifacts());
      if (path.endsWith("/artifacts/1/bundle")) return route.fulfill({ body: "saved report v1", contentType: "application/zip" });
      throw new Error(`Unexpected request ${request.method()} ${path}`);
    });
    const root = `/tasks/${taskId}`;
    await page.goto(`${root}/review${mode === "overview-all" ? "" : "/S1/q1"}`);
    if (mode === "individual") {
      for (const id of ["q1", "q3"]) {
        const card = page.locator(`[data-question-id="${id}"]`);
        await card.getByRole("button", { name: zh ? /待确认/ : /Confirm review/ }).click();
        if (id === "q1") await expect(card.getByRole("button", { name: zh ? /已确认/ : /Confirmed/ })).toBeVisible();
        else await expect(page).toHaveURL(`${root}/review`);
      }
    } else {
      await page.getByRole("button", { name: zh ? "全部确认" : "Confirm all", exact: true }).click();
      await expect.poll(() => writes.length).toBe(4);
    }
    const reviewWrites = mode === "individual" ? 2 : 4;
    await page.getByRole("button", { name: zh ? "查看结果分析" : "View Results & Analysis", exact: true }).click();
    await expect(page).toHaveURL(`${root}/results`);
    expect(writes).toHaveLength(reviewWrites);
    for (const suffix of ["/reports", "", "/questions", "/questions/q1", "/students", "/students/S1", "/visualizations"]) {
      await page.goto(`${root}/results${suffix}`);
      await expect(page.getByRole("heading", { name: zh ? "学情分析与导出" : "Performance Analysis & Exports" })).toBeVisible();
      if (suffix === "/visualizations") await expect(page.locator('[data-chart-export="question-performance"]')).toBeVisible();
      if (suffix === "/reports") await expect(page.getByRole("button", { name: zh ? "生成本版文件" : "Generate files" })).toBeVisible();
      await expect(page.locator("main").last()).not.toContainText(/AI\s*(?:分|score)|未人工|教师未操作|not reviewed|no teacher action|Unconfirmed/);
      await expect(page.locator("main").last()).not.toContainText(/[1-9]\d* 项待|[1-9]\d* awaiting|[1-9]\d* 个专家分歧/);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    }
    await page.screenshot({ path: testInfo.outputPath("analysis-after-confirmation.png"), fullPage: true });
    await page.goto(`${root}/review`);
    await expect(page.getByRole("region", { name: zh ? "待复核队列" : "Review queue" })).toContainText(zh ? "当前没有尚待处理" : "No unresolved");
    // Changing a confirmed grade updates both statistics and charts without a release.
    await page.goto(`${root}/review/S1/q1`);
    const first = page.locator('[data-question-id="q1"]');
    await first.getByRole("textbox", { name: zh ? "Q1 最终得分" : "Q1 final score" }).fill("9");
    await first.getByRole("button", { name: zh ? /待确认/ : /Confirm review/ }).click();
    await expect(page).toHaveURL(`${root}/review`);
    await page.getByRole("button", { name: zh ? "查看结果分析" : "View Results & Analysis", exact: true }).click();
    await page.goto(`${root}/results/visualizations`);
    await expect(page.locator('[data-chart-export="question-performance"]')).toBeVisible();
    await expect(page.locator("main").last()).toContainText("78%");
    expect(writes).toHaveLength(reviewWrites + 1);
    await page.goto(`${root}/results/reports`);
    reportGenerationRequested = true;
    await page.getByRole("button", { name: zh ? "生成本版文件" : "Generate files" }).click();
    await expect(page.getByRole("button", { name: zh ? "下载全部 ZIP" : "Download all ZIP" })).toBeVisible();
    expect(writes).toHaveLength(reviewWrites + 2);
    if (mode === "individual") expect(corrections[1].teacher_score).toBeNull();
    if (mode === "individual") {
      // A separately saved edit moves the generated report into history.
      reportDirty = true; task.workflow_revision += 1;
      const dialogs: string[] = [];
      page.on("dialog", dialog => { dialogs.push(dialog.message()); void dialog.accept(); });
      await page.reload();
      await expect(page.getByText(zh ? "最新报告尚未生成，请先生成再下载。" : "No latest report yet. Generate it before downloading.")).toBeVisible();
      await expect(page.getByRole("button", { name: zh ? "下载全部 ZIP" : "Download all ZIP" })).toHaveCount(0);
      const downloaded = page.waitForEvent("download");
      await page.getByRole("button", { name: zh ? "下载 ZIP" : "Download ZIP", exact: true }).click();
      expect((await downloaded).suggestedFilename()).toBe("Review consistency_2023-11-14_v1_reports.zip");
      expect(dialogs).toEqual([]);
      await page.getByRole("button", { name: zh ? "生成本版文件" : "Generate files" }).click();
      await expect(page.getByRole("button", { name: zh ? "下载全部 ZIP" : "Download all ZIP" })).toBeVisible();
      await expect(page.getByRole("button", { name: zh ? "下载 ZIP" : "Download ZIP", exact: true })).toBeVisible();
      await expect(page.locator("main").last()).not.toContainText(/过期|out of date|stale|regenerate report files/i);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath("latest-and-history.png"), fullPage: true });
      await page.getByRole("button", { name: zh ? "删除" : "Delete", exact: true }).click();
      await expect(page.getByText(zh ? "暂无历史报告。" : "No historical reports.")).toBeVisible();
      await expect(page.getByRole("button", { name: zh ? "下载全部 ZIP" : "Download all ZIP" })).toBeVisible();
      expect(dialogs).toHaveLength(1);
    }
    expect(errors).toEqual([]);
  });
}
