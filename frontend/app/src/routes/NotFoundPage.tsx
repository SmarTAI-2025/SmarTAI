import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";

export function NotFoundPage() {
  return (
    <EmptyState
      title="页面不存在"
      description="请返回教师工作台继续操作。"
      action={
        <Link to="/">
          <Button variant="secondary">返回总览</Button>
        </Link>
      }
    />
  );
}

