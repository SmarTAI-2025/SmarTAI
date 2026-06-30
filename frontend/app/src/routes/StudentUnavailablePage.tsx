import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";

export function StudentUnavailablePage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4">
      <EmptyState
        title="学生端暂未开放"
        description="当前 React 重构阶段只展示教师端批改主流程。"
        action={
          <Link to="/login">
            <Button variant="secondary">返回登录</Button>
          </Link>
        }
      />
    </main>
  );
}

