// Source preflight keeps 950s processing plus up to 60s PDF cleanup and 10s delivery.
// This is not the lifetime of a durable recognition job.
export const SOURCE_PREFLIGHT_TIMEOUT_MS = 1_020_000;
