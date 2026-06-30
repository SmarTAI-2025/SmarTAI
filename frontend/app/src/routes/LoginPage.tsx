import { ClipboardList } from "lucide-react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";

export function LoginPage() {
  return (
    <AuthFrame>
      <Card className="w-full max-w-md p-6">
        <div className="text-center">
          <ClipboardList className="mx-auto h-10 w-10 text-primary" />
          <h1 className="mt-3 text-2xl font-semibold">登录 SmarTAI</h1>
          <p className="mt-1 text-sm text-muted-foreground">进入教师端批改工作台。</p>
        </div>
        <form className="mt-6 grid gap-4">
          <Field label="用户名">
            <Input autoComplete="username" placeholder="username" />
          </Field>
          <Field label="密码">
            <Input autoComplete="current-password" placeholder="••••••••" type="password" />
          </Field>
          <Button type="button" className="w-full">
            登录
          </Button>
        </form>
        <div className="mt-5 text-center text-sm text-muted-foreground">
          还没有账号？{" "}
          <Link className="font-medium text-primary" to="/register">
            申请注册
          </Link>
        </div>
      </Card>
    </AuthFrame>
  );
}

function AuthFrame({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4">
      {children}
    </main>
  );
}

