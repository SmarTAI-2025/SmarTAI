export const authKeys = {
  me: ["auth", "me"] as const,
};

export const taskKeys = {
  all: ["tasks"] as const,
  list: () => ["tasks", "list"] as const,
  history: (queryKey: string) => ["tasks", "history", queryKey] as const,
  detail: (taskId: string) => ["tasks", "detail", taskId] as const,
  state: (taskId: string) => ["tasks", "state", taskId] as const,
  result: (taskId: string) => ["tasks", "result", taskId] as const,
  finalization: (taskId: string) => ["tasks", "finalization", taskId] as const,
  artifacts: (taskId: string) => ["tasks", "artifacts", taskId] as const,
  comments: (taskId: string) => ["tasks", "comments", taskId] as const,
};

export const tagKeys = {
  all: ["tags"] as const,
  list: () => ["tags", "list"] as const,
  search: (query: string) => ["tags", "search", query] as const,
};

export const courseKeys = {
  all: ["courses"] as const,
  list: () => ["courses", "list"] as const,
  search: (query: string) => ["courses", "search", query] as const,
};

export const courseMaterialKeys = {
  all: ["course-materials"] as const,
  list: (queryKey: string) => ["course-materials", "list", queryKey] as const,
  groups: (query: string) => ["course-materials", "groups", query] as const,
};

export const kbKeys = {
  all: ["kb"] as const,
  list: (taskId: string) => ["kb", "list", taskId] as const,
};

export const problemSourceKeys = {
  all: ["problem-sources"] as const,
  capabilities: (taskId: string) => ["problem-sources", "capabilities", taskId] as const,
  library: (taskId: string, scope: string, query: string) =>
    ["problem-sources", "library", taskId, scope, query] as const,
};


export const aiCompletionKeys = {
  all: ["ai-completions"] as const,
  preflight: (taskId: string) => ["ai-completions", taskId, "preflight"] as const,
  detail: (taskId: string, jobId: string) => ["ai-completions", taskId, jobId] as const,
};

export const gradingSetupKeys = {
  all: ["grading-setup"] as const,
  detail: (taskId: string) => ["grading-setup", taskId] as const,
};

export const expertKeys = {
  all: ["experts"] as const,
  list: () => ["experts", "list"] as const,
  catalog: () => ["experts", "catalog"] as const,
  stage: () => ["experts", "stage-options"] as const,
};

export const ocrProviderKeys = {
  all: ["ocr-providers"] as const,
  baiduConfiguration: () => ["ocr-providers", "baidu-unlimited-ocr"] as const,
};

// Personal knowledge and private admin caches remain separate from task data.
export const personalKnowledgeKeys = {
  usage: () => ["personal-knowledge", "storage", "usage"] as const,
};

export const adminKeys = {
  all: ["admin"] as const,
  users: (filter?: { role?: string; is_active?: boolean; search?: string; page?: number; page_size?: number }) =>
    ["admin", "users", filter ?? {}] as const,
  overview: () => ["admin", "overview"] as const,
  audit: () => ["admin", "audit"] as const,
};
