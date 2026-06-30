import type { ReactNode } from "react";
import { BarChart3, ClipboardList, FileText, Layers3, UserRound } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { SectionHeader } from "@/components/ui/Card";
import { cn } from "@/lib/cn";
import { TaskStepper } from "./TaskStepper";

type ResultContext = "overview" | "by-question" | "visualization" | "student-detail" | "question-detail";

const baseTabs = [
  {
    key: "overview",
    label: "总览",
    description: "班级概况",
    icon: ClipboardList,
    href: (taskId: string) => `/tasks/${taskId}/results`,
  },
  {
    key: "by-question",
    label: "按题分析",
    description: "题目维度",
    icon: Layers3,
    href: (taskId: string) => `/tasks/${taskId}/results?view=questions`,
  },
  {
    key: "visualization",
    label: "可视化",
    description: "图表探索",
    icon: BarChart3,
    href: (taskId: string) => `/tasks/${taskId}/results?view=charts`,
  },
] as const;

export function ResultsLayout({
  context,
  title,
  description,
  children,
}: {
  context: ResultContext;
  title: string;
  description: string;
  children: ReactNode;
}) {
  const { taskId = "draft", studentId = "student-a", questionId = "q1" } = useParams();

  const tabs = [
    ...baseTabs,
    {
      key: "student-detail",
      label: "学生详情",
      description: "跨题查看",
      icon: UserRound,
      href: () => `/tasks/${taskId}/results/${studentId}`,
    },
    {
      key: "question-detail",
      label: "题目详情",
      description: "全班作答",
      icon: FileText,
      href: () => `/tasks/${taskId}/questions/${questionId}`,
    },
  ];

  return (
    <div className="grid gap-5">
      <TaskStepper current="results" />
      <SectionHeader title={title} description={description} />
      <nav aria-label="结果分析视图" className="rounded-lg border bg-card p-2">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            const active = tab.key === context;
            return (
              <Link
                key={tab.key}
                to={tab.href(taskId)}
                className={cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 transition",
                  active
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
                aria-current={active ? "page" : undefined}
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="grid gap-0.5">
                  <span className="text-sm font-semibold">{tab.label}</span>
                  <span
                    className={cn(
                      "text-xs leading-4",
                      active ? "text-primary-foreground/80" : "text-muted-foreground",
                    )}
                  >
                    {tab.description}
                  </span>
                </span>
              </Link>
            );
          })}
        </div>
      </nav>
      {children}
    </div>
  );
}
