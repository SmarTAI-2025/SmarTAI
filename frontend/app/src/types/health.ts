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
}
