import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const locale of ["zh-CN", "en-US"]) {
  for (const editor of ["question", "answer"]) {
    for (const choice of ["stay", "discard", "save"] as const) {
      test(`mobile navigation keeps browser-back confirmation usable (${locale}, ${editor}, ${choice})`, async ({ page }, testInfo) => {
        const zh = locale === "zh-CN", errors: string[] = [], writes: string[] = [];
        page.on("pageerror", error => errors.push(error.message));
        page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
        await page.setViewportSize({ width: 390, height: 844 });
        await page.addInitScript(value => {
          localStorage.setItem("smartai_token", "synthetic-mobile-layers");
          localStorage.setItem("smartai_locale", value);
        }, locale);
        const task = { task_id: "mobile-layers", name: "Synthetic mobile review", status: editor === "question" ? "problems_ready" : "submissions_ready", workflow_revision: 1,
          problem_count: 1, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0,
          problem_data: { q1: { q_id: "q1", number: "1", type: "concept", stem: "Original question", criterion: "One point", max_score: 1,
            review_status: "confirmed", max_score_review_status: "confirmed" } },
          student_data: { S1: { stu_id: "S1", stu_name: "Synthetic student", identity_status: "matched",
            stu_ans: [{ answer_id: "a1", q_id: "q1", number: "1", type: "concept", content: "Original answer", review_status: "confirmed", flag: [] }] } } };
        await page.route(`${backend}/**`, async route => {
          const request = route.request(), path = new URL(request.url()).pathname, json = (value: unknown) => route.fulfill({ json: value });
          if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
          if (path === "/auth/me") return json({ id: "mobile-teacher", username: "Synthetic teacher", role: "teacher", is_active: true });
          if (path === "/auth/activity") return json({ token: "synthetic-mobile-layers" });
          if (request.method() !== "GET") { writes.push(path); return route.fulfill({ status: 500, json: { detail: "Unexpected business write" } }); }
          if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: 1, problem_source: null, submission_sources: {} });
          if (path === `/tasks/${task.task_id}` || path.endsWith("/state")) return json(task);
          return json([]);
        });

        const overview = `/tasks/${task.task_id}/${editor === "question" ? "questions" : "submissions"}`;
        await page.goto(overview);
        const detailPrefix = `/tasks/${task.task_id}/${editor === "question" ? "questions/q1/content" : "students/S1"}`;
        await page.locator(`a[href^="${detailPrefix}"]`).first().click();
        const detail = page.url();
        const card = page.locator(`[data-question-id="${editor === "question" ? "q1" : "a1"}"]`);
        await card.getByRole("button", { name: editor === "question" ? (zh ? "题目 · 修改" : "Question · Edit") : (zh ? "修改" : "Edit"), exact: true }).click();
        const input = card.locator("textarea").first();
        await input.fill("Unsaved private edit");
        await page.getByRole("button", { name: zh ? "打开导航" : "Open navigation", exact: true }).click();
        const navigation = page.getByRole("dialog", { name: zh ? "移动端导航" : "Mobile navigation", exact: true });
        await expect(navigation).toBeVisible();
        // Trigger a real browser history POP, without choosing a menu link.
        await page.evaluate(() => history.back());
        const dialog = page.getByRole("alertdialog", { name: zh ? "有未暂存修改" : "Unsaved changes", exact: true });
        await expect(dialog).toBeVisible();
        await expect(page).toHaveURL(detail);
        const stay = dialog.getByRole("button", { name: zh ? "继续编辑" : "Keep editing", exact: true });
        const discard = dialog.getByRole("button", { name: zh ? "不暂存并离开" : "Leave without saving", exact: true });
        const save = dialog.getByRole("button", { name: zh ? "暂存并离开" : "Save draft and leave", exact: true });
        for (const button of [stay, discard, save]) {
          // Do not let automatic click retries hide a covered confirmation.
          expect(await button.evaluate(element => {
            const rect = element.getBoundingClientRect();
            return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
          })).toBe(true);
        }
        await expect(stay).toBeFocused();
        await page.keyboard.press("Tab"); await expect(discard).toBeFocused();
        await page.keyboard.press("Tab"); await expect(save).toBeFocused();
        await page.keyboard.press("Tab"); await expect(stay).toBeFocused();
        await page.screenshot({ path: testInfo.outputPath("back-confirmation-above-navigation.png") });
        await ({ stay, discard, save })[choice].click();
        await expect(dialog).toHaveCount(0);
        if (choice === "stay") {
          await expect(input).toHaveValue("Unsaved private edit");
          await expect(page).toHaveURL(detail);
          await navigation.getByRole("button", { name: zh ? "关闭导航" : "Close navigation", exact: true }).click();
        } else {
          await expect(page).toHaveURL(new RegExp(`${overview}$`));
          await navigation.getByRole("button", { name: zh ? "关闭导航" : "Close navigation", exact: true }).click();
          await page.locator(`a[href^="${detailPrefix}"]`).first().click();
          if (choice === "save") await expect(card.locator("textarea").first()).toHaveValue("Unsaved private edit");
          else await expect(card.locator("textarea")).toHaveCount(0);
        }
        expect(writes).toEqual([]);
        expect(errors).toEqual([]);
      });
    }
  }
}
