import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "h-9 rounded-md border bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20",
        className,
      )}
      {...props}
    />
  );
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(
        "min-h-24 rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20",
        className,
      )}
      {...props}
    />
  );
}

