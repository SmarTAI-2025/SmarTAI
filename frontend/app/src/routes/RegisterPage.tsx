import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";

export function RegisterPage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-md p-6">
        <h1 className="text-center text-2xl font-semibold">申请注册</h1>
        <p className="mt-2 text-center text-sm leading-6 text-muted-foreground">
          当前注册暂未开放，仅限受邀测试账号使用。提交后会显示管理员联系提示。
        </p>
        <form className="mt-6 grid gap-4">
          <Field label="用户名">
            <Input placeholder="username" />
          </Field>
          <Field label="邮箱">
            <Input placeholder="you@example.com" type="email" />
          </Field>
          <Field label="邀请码">
            <Input placeholder="暂未开放" />
          </Field>
          <Button type="button" className="w-full">
            提交申请
          </Button>
        </form>
        <div className="mt-5 text-center text-sm text-muted-foreground">
          已有账号？{" "}
          <Link className="font-medium text-primary" to="/login">
            返回登录
          </Link>
        </div>
      </Card>
    </main>
  );
}

