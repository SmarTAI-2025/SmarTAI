import { ArrowRight, Eye } from "lucide-react";
import { Link } from "react-router-dom";
export function MatrixViewLink({ to, locale, iconOnly = false }: { to: string; locale: string; iconOnly?: boolean }) {
  if (iconOnly) return <Link to={to} title={locale === "zh-CN" ? "查看" : "View"} aria-label={locale === "zh-CN" ? "查看" : "View"} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-primary hover:bg-primary/10 focus-visible:ring-2 focus-visible:ring-ring"><Eye aria-hidden="true" className="h-4 w-4" /></Link>;
  return <Link to={to} className="inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold text-primary underline underline-offset-4 outline-none focus-visible:ring-2 focus-visible:ring-ring">
    {locale === "zh-CN" ? "查看" : "View"}<ArrowRight aria-hidden="true" className="h-3.5 w-3.5" />
  </Link>;
}
