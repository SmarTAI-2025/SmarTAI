import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useVerifyRegistration } from "@/api/hooks";
import { AuthCard, AuthError, AuthFrame } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";

export function RegisterVerifyPage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const location = useLocation();
  const verify = useVerifyRegistration();
  const [confirmed, setConfirmed] = useState(false);
  const token = new URLSearchParams(location.hash.replace(/^#/, "")).get("token") ?? "";

  async function handleConfirm() {
    try {
      await verify.mutateAsync(token);
      setConfirmed(true);
    } catch {
      // The API error is rendered below without consuming the token locally.
    }
  }

  return <AuthFrame><AuthCard><h1 className="text-[27px] font-semibold">{zh ? "确认教师账号" : "Confirm teacher account"}</h1>{confirmed ? <p className="mt-5 text-sm leading-6">{zh ? "注册完成，请返回登录。" : "Registration complete. Return to sign in."}</p> : <><p className="mt-3 text-sm leading-6 text-muted-foreground">{zh ? "点击按钮完成邮箱验证。打开链接不会自动创建账号。" : "Click the button to finish email verification. Opening the link does not create an account."}</p>{verify.error ? <AuthError message={String(verify.error)} /> : null}<Button className="mt-6 w-full" disabled={!token || verify.isPending} onClick={handleConfirm}>{zh ? "确认并完成注册" : "Confirm registration"}</Button></>}<div className="mt-5 border-t pt-5 text-center text-sm"><Link className="font-semibold text-primary hover:underline" to="/login">{zh ? "返回登录" : "Back to sign in"}</Link></div></AuthCard></AuthFrame>;
}
