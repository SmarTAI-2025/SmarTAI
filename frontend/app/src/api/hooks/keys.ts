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

export const materialImportKeys = {
  all: ["material-imports"] as const,
  detail: (taskId: string, jobId: string) => ["material-imports", taskId, jobId] as const,
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
};

export const analyticsKeys = {
  all: ["analytics"] as const,
  perQuestion: (taskId: string, qId: string) => ["analytics", "perQuestion", taskId, qId] as const,
};

export const healthKeys = {
  root: ["health", "root"] as const,
  status: ["health", "status"] as const,
};

// The normalized education/admin/personal-knowledge workspaces are dormant in
// the production router. Their cache keys stay explicitly namespaced so they
// can never alias the Figma task/course presentation facade above.
export const personalKnowledgeKeys = {
  all: ["personal-knowledge"] as const,
  list: () => ["personal-knowledge", "list"] as const,
};

export const educationCourseKeys = {
  all: ["education", "courses"] as const,
  list: () => ["education", "courses", "list"] as const,
  detail: (courseId: string) => ["education", "courses", "detail", courseId] as const,
};

export const assignmentKeys = {
  all: ["education", "assignments"] as const,
  list: (courseId: string) => ["education", "assignments", "list", courseId] as const,
  detail: (assignmentId: string) => ["education", "assignments", "detail", assignmentId] as const,
  questions: (assignmentId: string) =>
    ["education", "assignments", assignmentId, "questions"] as const,
};

export const submissionKeys = {
  all: ["education", "submissions"] as const,
  list: (assignmentId: string) => ["education", "submissions", "list", assignmentId] as const,
  detail: (submissionId: string) => ["education", "submissions", "detail", submissionId] as const,
};

export const gradingRunKeys = {
  all: ["education", "grading-runs"] as const,
  list: (assignmentId: string) => ["education", "grading-runs", "list", assignmentId] as const,
  detail: (runId: string) => ["education", "grading-runs", "detail", runId] as const,
  reviewQueue: (assignmentId: string) =>
    ["education", "grading-runs", "review", assignmentId] as const,
};

export const resultKeys = {
  all: ["education", "results"] as const,
  summary: (assignmentId: string) => ["education", "results", "summary", assignmentId] as const,
  student: (assignmentId: string, studentId: string) =>
    ["education", "results", "student", assignmentId, studentId] as const,
  perQuestion: (assignmentId: string) =>
    ["education", "results", "per-question", assignmentId] as const,
  me: (assignmentId: string) => ["education", "results", "me", assignmentId] as const,
};

export const adminKeys = {
  all: ["admin"] as const,
  users: (filter?: { role?: string; is_active?: boolean }) =>
    ["admin", "users", filter ?? {}] as const,
  invites: () => ["admin", "invites"] as const,
};
