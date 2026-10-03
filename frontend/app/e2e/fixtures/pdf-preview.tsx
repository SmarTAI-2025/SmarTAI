// Dev/E2E-only harness: uses the production panel and splitter, no backend or models.
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { OriginalFilePreviewPanel } from "@/components/tasks/OriginalFilePreviewPanel";
import { SourceComparisonWorkspace } from "@/components/tasks/SourceComparisonWorkspace";
import { messages } from "@/i18n/messages";
import "@/styles/globals.css";

function PreviewFixture() {
  const [file, setFile] = useState("multiple");
  const [open, setOpen] = useState(true);
  const [initialPage, setInitialPage] = useState(1);
  const [question, setQuestion] = useState(1);
  return <main className="mx-auto max-w-[1480px] p-4">
    <h1 className="mb-3 text-lg font-bold">题目审核 · PDF 本地隔离验收</h1>
    <nav className="mb-4 flex flex-wrap gap-2">
      {["single", "multiple", "long", "mixed", "broken", "slow"].map((name) => <button key={name} className="rounded border bg-card px-3 py-1" onClick={() => { setFile(name); setInitialPage(1); setOpen(true); }}>{name}</button>)}
      <button className="rounded border px-3 py-1" onClick={() => setOpen(!open)}>Toggle preview</button>
    </nav>
    <SourceComparisonWorkspace open={open} separatorLabel="调整原文件宽度" preview={
      <OriginalFilePreviewPanel descriptor={{ source_id: "synthetic", file_id: file, display_name: `${file}.pdf`, mime_type: "application/pdf", size_bytes: 1000, status: "available", preview_kind: "pdf" }} displayName={`${file}.pdf`} previewKind="pdf" loadState="ready" errorCode={null} previewUrl={`./${file}.pdf`} initialPage={initialPage} onClose={() => setOpen(false)} onRetry={() => setOpen(true)} t={(key) => messages["zh-CN"][key]} />
    }>
      <section className="px-4" data-testid="question-content">
        <h2 className="text-lg font-bold">题目 {question} · 教师复核</h2>
        <p className="my-3">右侧题目选择与原文件阅读页码互相独立。以下仅为无敏感数据测试控件。</p>
        <button className="rounded border px-3 py-2" onClick={() => { setQuestion(2); setInitialPage(8); }}>定位题目 2（第 8 页）</button>
        <button className="m-2 rounded border px-3 py-2" onClick={() => { setQuestion(1); setInitialPage(1); }}>定位题目 1（第 1 页）</button>
        {Array.from({ length: 20 }, (_, i) => <p className="my-10" key={i}>复核内容 {i + 1} · 用于确认外层页面可以正常滚动。</p>)}
      </section>
    </SourceComparisonWorkspace>
  </main>;
}
createRoot(document.getElementById("root")!).render(<PreviewFixture />);
