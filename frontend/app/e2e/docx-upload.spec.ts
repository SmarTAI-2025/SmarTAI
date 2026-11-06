import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { currentLogin } from "./fixtures/current-auth";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const bytes = readFileSync(new URL("./fixtures/native-word.docx", import.meta.url));
const mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

for (const [width, locale] of [[1440, "en-US"], [390, "zh-CN"]] as const) {
  test(`native Word upload, download and question drop at ${width}px`, async ({ page, request }, info) => {
    const token = await currentLogin(page, "teacher", `word-${width}`);
    const headers = { Authorization: `Bearer ${token}` };
    const zh = locale === "zh-CN";
    const name = "Lesson.DOCX";
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(({ token, locale }) => {
      localStorage.setItem("smartai_token", token);
      localStorage.setItem("smartai_locale", locale);
    }, { token, locale });
    await page.goto("/knowledge-base");
    await expect(page.getByRole("heading", { name: zh ? "课程资料库" : "Course Library" })).toBeVisible();
    await page.getByRole("button", { name: zh ? "上传资料" : "Upload", exact: true }).click();
    const dialog = page.getByRole("dialog");
    const picker = dialog.locator('input[type="file"]');
    await expect(picker).toHaveAttribute("accept", /\.docx/);
    await picker.setInputFiles({ name, mimeType: mime, buffer: bytes });
    await dialog.getByRole("checkbox", { name: zh ? /仅提取已有文字/ : /Existing text only/ }).check();
    const uploaded = page.waitForResponse(r => r.url().endsWith("/course-materials/") && r.request().method() === "POST");
    await dialog.getByRole("button", { name: zh ? "上传资料" : "Upload", exact: true }).click();
    const response = await uploaded;
    expect(response.ok(), await response.text()).toBeTruthy();
    const material = await response.json();
    expect(material.parse_status).toBe("ready");
    const search = await request.post(`${backend}/knowledge/search`, {
      headers, data: { query: "def inc", document_ids: [material.document_id] },
    });
    expect(search.ok()).toBeTruthy();
    const matches = (await search.json()).matches;
    expect(matches.length).toBeGreaterThan(0);
    const citation = matches[0].citation;
    const original = await request.get(`${backend}/knowledge/documents/${material.document_id}/citations/${citation.chunk_id}/download`, { headers });
    expect(original.ok()).toBeTruthy();
    expect(await original.body()).toEqual(bytes);
    await expect(dialog).toBeHidden();
    const task = await request.post(`${backend}/tasks/`, { headers, data: { name: `Native Word ${width}` } });
    expect(task.ok()).toBeTruthy();
    await page.goto(`/tasks/${(await task.json()).task_id}/upload/problems`);
    const drop = page.locator('[aria-label]').filter({ has: page.locator('input[id^="problem-source-file-"]') }).last();
    await expect(drop.locator("input")).toHaveAttribute("accept", /\.docx/);
    const transfer = await page.evaluateHandle(({ data, name, mime }) => {
      const dt = new DataTransfer();
      dt.items.add(new File([Uint8Array.from(data)], name, { type: mime }));
      return dt;
    }, { data: [...bytes], name, mime });
    await drop.dispatchEvent("drop", { dataTransfer: transfer });
    await expect(drop.getByText(name, { exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`word-${width}.png`) });
    expect(errors).toEqual([]);
  });
}
