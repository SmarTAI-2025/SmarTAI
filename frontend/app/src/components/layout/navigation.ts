import type { MessageKey } from "@/i18n/messages";

export interface PrimaryNavigationItem {
  to: string;
  labelKey: MessageKey;
}

/**
 * Single source of truth for the desktop and mobile primary navigation.
 * Settings and model configuration intentionally live in the account area.
 */
export const PRIMARY_NAVIGATION = [
  { to: "/", labelKey: "workspace" },
  { to: "/tasks/new", labelKey: "newTask" },
  { to: "/history", labelKey: "history" },
  { to: "/knowledge-base", labelKey: "courseLibrary" },
] as const satisfies readonly PrimaryNavigationItem[];
