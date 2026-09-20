import { Check, Inbox } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { AuthCard, AuthFrame } from "@/components/auth/AuthFrame";
import { useI18n } from "@/i18n/I18nProvider";
import {
  clearPasswordResetRequestMarker,
  readPasswordResetRequestMarker,
} from "@/lib/passwordResetRequestFlow";

export function PasswordResetCheckEmailPage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const [now, setNow] = useState(Date.now());
  const marker = readPasswordResetRequestMarker(now);
  const expiresAt = marker?.expiresAt;
  useEffect(() => {
    if (!expiresAt) return undefined;
    const timer = window.setTimeout(
      () => setNow(Date.now()),
      Math.max(0, expiresAt - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [expiresAt]);
  if (!marker) return <Navigate to="/forgot-password" replace />;
  return (
    <AuthFrame>
      <AuthCard>
        <div className="flex items-center gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
            <Inbox aria-hidden="true" size={21} />
          </span>
          <div>
            <p className="text-xs font-semibold text-primary">{zh ? "检查邮箱" : "Check your email"}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{zh ? "不透露账号是否存在" : "Account existence stays private"}</p>
          </div>
        </div>
        <h1 className="mt-5 text-[27px] font-semibold">{zh ? "重置请求已受理" : "Reset request accepted"}</h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {zh
            ? "如果该邮箱符合条件，我们会发送密码重置邮件。无论账号存在、停用或不存在，此页面都会显示相同结果。"
            : "If the email is eligible, we send a reset message. This page looks the same whether the account exists, is disabled, or does not exist."}
        </p>
        <div className="mt-5 rounded-[10px] border bg-muted/30 p-4">
          <ol className="grid gap-3 text-sm leading-6">
            {[
              zh ? "链接约 30 分钟有效。" : "The link is valid for about 30 minutes.",
              zh ? "只使用最新一封邮件中的链接。" : "Use only the link in the newest email.",
              zh ? "没有看到时，请检查垃圾邮件和推广分类。" : "Check spam and promotions if it is missing.",
            ].map((item) => (
              <li key={item} className="flex gap-2.5">
                <span className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
                  <Check aria-hidden="true" size={12} strokeWidth={3} />
                </span>
                <span>{item}</span>
              </li>
            ))}
          </ol>
        </div>
        <div className="mt-6 grid gap-3">
          <Link
            className="inline-flex h-11 w-full items-center justify-center rounded-md border bg-card px-3 text-sm font-medium text-foreground hover:bg-muted"
            to="/forgot-password"
            onClick={clearPasswordResetRequestMarker}
          >
            {zh ? "重新申请" : "Request again"}
          </Link>
          <Link
            className="text-center text-sm font-semibold text-primary hover:underline"
            to="/login"
            onClick={clearPasswordResetRequestMarker}
          >
            {zh ? "返回登录" : "Back to sign in"}
          </Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}
