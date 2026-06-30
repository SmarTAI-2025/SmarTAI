export type Locale = "zh-CN" | "en-US";

export const messages = {
  "zh-CN": {
    appName: "SmarTAI",
    dashboard: "总览",
    history: "历史",
    newTask: "新建任务",
    experts: "BYOK 专家",
    settings: "设置",
    logout: "退出",
    theme: "主题",
    language: "语言",
    light: "浅色",
    dark: "深色",
    system: "跟随系统",
  },
  "en-US": {
    appName: "SmarTAI",
    dashboard: "Dashboard",
    history: "History",
    newTask: "New Task",
    experts: "BYOK Experts",
    settings: "Settings",
    logout: "Logout",
    theme: "Theme",
    language: "Language",
    light: "Light",
    dark: "Dark",
    system: "System",
  },
} as const;

export type MessageKey = keyof typeof messages["zh-CN"];

