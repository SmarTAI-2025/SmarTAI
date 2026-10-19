import { ArrowLeft, LoaderCircle, RotateCcw, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FrontierExitLink } from "@/components/auth/FrontierExitLink";
import { createFrontierDemoSession } from "@/api/auth";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { SmarTAIAppMark, SmarTAIWordmark } from "@/components/brand/SmarTAIBrand";
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
        <FrontierExitLink to="/" className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-600 hover:text-blue-700">
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          {zh ? "返回项目展示" : "Back to showcase"}
        </FrontierExitLink>
        <LanguageToggle className="border-slate-300 bg-white/85" />
      </div>
      <section className="frontier-entry-card frontier-entry-card-branded" aria-live="polite">
        <div className="frontier-entry-brand" aria-label="SmarTAI" role="img">
          <span className="frontier-entry-brand-app" aria-hidden="true"><SmarTAIAppMark finish="silver" /></span>
          <SmarTAIWordmark tone="white" alt="" className="frontier-entry-brand-wordmark" />
        </div>
        <span className="frontier-entry-icon">
          {error ? <RotateCcw aria-hidden="true" /> : <LoaderCircle aria-hidden="true" className="animate-spin" />}
        </span>
        <p className="frontier-eyebrow">{zh ? "产品演示" : "Product demo"}</p>
        <h1>{error ? (zh ? "暂时无法进入 Demo" : "The demo could not start") : (zh ? "正在进入演示" : "Starting the demo")}</h1>
        <p>
          {error
            ? errorMessage
            : zh
              ? "正在准备演示，请稍候。"
              : "Preparing the demo. Please wait."}
        </p>
        {error ? (
          <Button type="button" className="mt-5 h-11 px-5" onClick={() => setAttempt((value) => value + 1)}>
            <RotateCcw aria-hidden="true" className="h-4 w-4" />
            {zh ? "重试" : "Try again"}
          </Button>
        ) : (
          <span className="frontier-entry-status mt-5 inline-flex items-center gap-2 text-xs font-semibold">
            <ShieldCheck aria-hidden="true" className="h-4 w-4" />
            {zh ? "示例作业 · 识别与批改" : "Sample coursework · recognition and grading"}
          </span>
        )}
      </section>
    </main>
  );
}

function entryErrorMessage(error: { code: string | null; status: number }, zh: boolean) {
  if (error.code === "frontier_demo_disabled") {
    return zh
      ? "演示暂不可用，请稍后重试。"
      : "The demo is unavailable. Please try again later.";
  }
  if (error.code === "frontier_demo_provider_unavailable") {
    return zh
      ? "演示暂不可用，请稍后重试。"
      : "The demo is unavailable. Please try again later.";
  }
  if (error.code === "frontier_demo_session_cooldown") {
    return zh
      ? "演示服务繁忙，请稍后重试。"
      : "The demo is busy. Please try again shortly.";
  }
  if (error.code === "frontier_demo_daily_limit_reached") {
    return zh
      ? "今日演示体验名额已用完，请稍后再试。"
      : "Today’s demo capacity has been reached. Please try again later.";
  }
  if (error.status === 0) {
    return zh
      ? "无法连接演示服务，请检查网络后重试。"
      : "Cannot connect to the demo. Check your connection and try again.";
  }
  return zh
    ? "演示暂不可用，请稍后重试。"
    : "The demo is unavailable. Please try again later.";
}
