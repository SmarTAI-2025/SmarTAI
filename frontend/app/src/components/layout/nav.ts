import {
  BookOpenCheck,
  ClipboardList,
  GraduationCap,
  LayoutDashboard,
  ListChecks,
  type LucideIcon,
  Users,
} from "lucide-react";
import type { UserRole } from "@/types/auth";

export interface NavItem {
  to: string;
  labelKey: string;
  icon: LucideIcon;
}

/**
 * Role-specific navigation. Each role gets a distinct nav set against the same
 * AppShell styling; the shell renders whichever list matches the current role.
 * Teachers manage courses/assignments/grading; students submit and view
 * released results; admins manage users/invites/system.
 */
export const ROLE_NAV: Record<UserRole, NavItem[]> = {
  teacher: [
    { to: "/teacher", labelKey: "dashboard", icon: LayoutDashboard },
    { to: "/teacher/courses", labelKey: "courses", icon: BookOpenCheck },
    { to: "/teacher/grading", labelKey: "grading", icon: ListChecks },
    { to: "/teacher/knowledge-base", labelKey: "knowledgeBase", icon: ClipboardList },
  ],
  student: [
    { to: "/student", labelKey: "dashboard", icon: LayoutDashboard },
    { to: "/student/courses", labelKey: "courses", icon: GraduationCap },
    { to: "/student/results", labelKey: "myResults", icon: ListChecks },
  ],
  admin: [
    { to: "/admin", labelKey: "dashboard", icon: LayoutDashboard },
    { to: "/admin/users", labelKey: "users", icon: Users },
    { to: "/admin/invites", labelKey: "invites", icon: ClipboardList },
  ],
};

/** Home path per role — wrong-role URLs redirect here without clearing session. */
export function homeForRole(role: UserRole): string {
  if (role === "admin") return "/admin";
  if (role === "student") return "/student";
  return "/teacher";
}

export function navForRole(role: UserRole): NavItem[] {
  return ROLE_NAV[role] ?? [];
}
