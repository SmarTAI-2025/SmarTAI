import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/** Shared desktop proportion for the S05 and R01 matrix workspaces. */
export function MatrixQueueWorkspace({
  matrix,
  queue,
  className,
}: {
  matrix: ReactNode;
  queue: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_280px]", className)}>
      {matrix}
      {queue}
    </div>
  );
}
