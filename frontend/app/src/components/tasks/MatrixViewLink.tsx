import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
export function MatrixViewLink({ to, locale }: { to: string; locale: string }) {
  return <Link to={to} className="inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold text-primary underline underline-offset-4 outline-none focus-visible:ring-2 focus-visible:ring-ring">
    {locale === "zh-CN" ? "查看" : "View"}<ArrowRight aria-hidden="true" className="h-3.5 w-3.5" />
  </Link>;
}
