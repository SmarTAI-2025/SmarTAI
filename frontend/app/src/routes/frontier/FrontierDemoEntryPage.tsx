import { ArrowLeft, LoaderCircle, RotateCcw, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { createFrontierDemoSession } from "@/api/auth";
import { normalizeAPIError } from "@/api/client";
import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";
import "./frontier.css";

export function FrontierDemoEntryPage() {
  const navigate = useNavigate();
  const { locale } = useI18n();
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    createFrontierDemoSession()
      .then(() => {
        if (!cancelled) navigate("/frontier/live", { replace: true });
      })
      .catch((caught) => {
        if (!cancelled) setError(normalizeAPIError(caught).message);
      });
    return () => { cancelled = true; };
  }, [attempt, navigate]);

  const zh = locale === "zh-CN";
  return (
    <main className="frontier-entry-page">
      <div className="frontier-entry-toolbar">
        <Link to="/frontier" className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-600 hover:text-blue-700">
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          {zh ? "返回项目展示" : "Back to showcase"}
        </Link>
        <LanguageToggle className="border-slate-300 bg-white/85" />
      </div>
      <section className="frontier-entry-card" aria-live="polite">
        <span className="frontier-entry-icon">
          {error ? <RotateCcw aria-hidden="true" /> : <LoaderCircle aria-hidden="true" className="animate-spin" />}
        </span>
        <p className="frontier-eyebrow">AWS From Idea to Frontier</p>
        <h1>{error ? (zh ? "暂时无法进入 Demo" : "The demo could not start") : (zh ? "正在进入真实 Demo" : "Entering the live demo")}</h1>
        <p>
          {error
            ? error
            : zh
              ? "后端正在签发免密码短时会话。Gemini 密钥不会发送到浏览器。"
              : "The backend is issuing a short-lived passwordless session. The Gemini key is never sent to the browser."}
        </p>
        {error ? (
          <Button type="button" className="mt-5 h-11 px-5" onClick={() => setAttempt((value) => value + 1)}>
            <RotateCcw aria-hidden="true" className="h-4 w-4" />
            {zh ? "重试" : "Try again"}
          </Button>
        ) : (
          <span className="mt-5 inline-flex items-center gap-2 text-xs font-semibold text-blue-700">
            <ShieldCheck aria-hidden="true" className="h-4 w-4" />
            {zh ? "合成数据 · 真实 API / OCR / 批改" : "Synthetic data · real API / OCR / grading"}
          </span>
        )}
      </section>
    </main>
  );
}
