import { ArrowLeft, LoaderCircle, RotateCcw, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { createFrontierDemoSession } from "@/api/auth";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";
import "./frontier.css";

export function FrontierDemoEntryPage() {
  const navigate = useNavigate();
  const { locale } = useI18n();
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState<{ code: string | null; status: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    createFrontierDemoSession()
      .then(() => {
        if (!cancelled) navigate("/frontier/live", { replace: true });
      })
      .catch((caught) => {
        if (!cancelled) {
          const normalized = normalizeAPIError(caught);
          setError({ code: getAPIErrorCode(normalized), status: normalized.status });
        }
      });
    return () => { cancelled = true; };
  }, [attempt, navigate]);

  const zh = locale === "zh-CN";
  const errorMessage = error ? entryErrorMessage(error, zh) : null;
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
        <p className="frontier-eyebrow">{zh ? "SmarTAI · 真实产品 Demo" : "SmarTAI · Live product demo"}</p>
        <h1>{error ? (zh ? "暂时无法进入 Demo" : "The demo could not start") : (zh ? "正在进入真实 Demo" : "Entering the live demo")}</h1>
        <p>
          {error
            ? errorMessage
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

function entryErrorMessage(error: { code: string | null; status: number }, zh: boolean) {
  if (error.code === "frontier_demo_disabled") {
    return zh
      ? "此环境尚未启用 Demo 服务。请返回项目展示，或联系演示维护者确认后端配置。"
      : "The Demo service is not enabled in this environment. Return to the showcase or ask the demo maintainer to check the backend configuration.";
  }
  if (error.code === "frontier_demo_provider_unavailable") {
    return zh
      ? "Demo 模型服务尚未配置完成。请稍后再试或联系演示维护者。"
      : "The Demo model service is not configured yet. Try again later or contact the demo maintainer.";
  }
  if (error.code === "frontier_demo_session_cooldown") {
    return zh
      ? "刚刚已有访客进入 Demo。请等待几秒后重试。"
      : "Another visitor just entered the Demo. Wait a few seconds and try again.";
  }
  if (error.code === "frontier_demo_daily_limit_reached") {
    return zh
      ? "今日 Demo 会话已达到上限。请联系演示维护者安排访问。"
      : "The Demo has reached its session limit for today. Contact the demo maintainer to arrange access.";
  }
  if (error.status === 0) {
    return zh
      ? "无法连接到 Demo 后端。请确认本地后端已启动，然后重试。"
      : "The Demo backend could not be reached. Confirm that the local backend is running, then try again.";
  }
  return zh
    ? "Demo 服务暂时无法完成会话创建。请稍后重试。"
    : "The Demo service could not create a session. Try again shortly.";
}
