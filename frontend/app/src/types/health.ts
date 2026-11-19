export interface RootStatus {
  message: string;
  engine: string;
  status: string;
}

export interface HealthStatus {
  status: "healthy" | string;
  engine: string;
  memory_usage_mb: number;
  cpu_percent: number;
  workflow_timeout_minutes?: WorkflowTimeoutMinutes;
}

export interface WorkflowTimeoutMinutes {
  question_preparation: number;
  submission_recognition: number;
  grading: number;
  knowledge_ingestion: number;
}
