import { GraduationCap } from "lucide-react";
import { Link } from "react-router-dom";
import { AuthCard, AuthFrame } from "@/components/auth/AuthFrame";
import { useI18n } from "@/i18n/I18nProvider";

export function StudentUnavailablePage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  return (
    <AuthFrame>
      <AuthCard>
        <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
          <GraduationCap aria-hidden="true" size={21} />
        </span>
        <h1 className="mt-5 text-[27px] font-semibold tracking-[-0.025em]">
          {zh ? "学生端尚未开放" : "Student workspace is not available yet"}
        </h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {zh
            ? "目前仅支持教师使用。"
            : "SmarTAI is currently available to teachers only."}
        </p>
        <Link
          to="/login"
          className="mt-7 inline-flex h-11 w-full items-center justify-center rounded-md border bg-card px-3 text-sm font-medium text-foreground outline-none transition hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          {zh ? "返回登录" : "Back to sign in"}
        </Link>
      </AuthCard>
    </AuthFrame>
  );
}
