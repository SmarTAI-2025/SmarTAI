"""
SmarTAI backend settings via Pydantic BaseSettings.
Loaded from environment variables or .env file.
"""
from __future__ import annotations

import os
from typing import Optional, Literal
from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings, loaded from env vars."""

    # ─── Engine toggle (v1 = old routers, v2 = new agents/skills/tools) ────────
    grading_engine: Literal["v1", "v2"] = "v2"

    # ─── Default LLM provider (fallback if no BYOK keys configured) ────────────
    default_provider: Literal["gemini", "openai", "zhipu", "anthropic"] = "gemini"

    # Gemini
    # NEVER hardcode an API key here — keys must come from env vars or BYOK only.
    # If both env var and BYOK are unset, gemini provider stays unregistered and
    # ExpertRegistry.pick_default() returns None, which surfaces as a 503 to the
    # user with a clear "Add an API key first" message.
    gemini_api_key: Optional[str] = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

    # OpenAI-compatible (Zhipu, OpenAI, etc.)
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY", "")
    openai_api_base: str = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")

    # Zhipu
    zhipu_api_key: Optional[str] = os.getenv("ZHIPU_API_KEY", "")
    zhipu_api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-4.5-air"

    # Anthropic
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = "claude-sonnet-4-20250514"

    # ─── Network proxy (for accessing Google/OpenAI APIs behind GFW) ──────────
    # Set to "" to disable. Clash Verge default: http://127.0.0.1:7897
    http_proxy: str = os.getenv("HTTP_PROXY", "http://127.0.0.1:7897")
    https_proxy: str = os.getenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    # ─── Concurrency & performance ─────────────────────────────────────────────
    max_concurrent_jobs: int = 10
    # Fallback semaphore size when a ProviderConfig has no explicit max_concurrent.
    # GLM-4.5-Air rate limit is ~5/min; OpenAI / Gemini paid tiers commonly take
    # 10+ — set fallback conservatively. Per-key override comes from BYOK config
    # (ProviderConfig.max_concurrent).
    max_concurrent_llm_per_provider: int = 5
    llm_timeout: int = 600  # seconds
    llm_max_retries: int = 3
    # When the LLM returns a 429 / quota exceeded error AND the provider's
    # response carries a retry-after hint (Gemini's `retryDelay: '23s'` or the
    # standard `Retry-After` header), we honor the server's wait suggestion and
    # retry up to this many additional attempts on top of `llm_max_retries`.
    # Generic transient errors (timeout/5xx) still use exponential backoff with
    # `llm_max_retries`. Set to 0 to disable the dedicated rate-limit retry
    # path. Default 6 covers a sustained quota burst over ~2-3 minutes.
    llm_rate_limit_max_retries: int = 6
    # Hard cap on a single retry sleep (seconds). Gemini occasionally suggests
    # 30-40s; OpenAI rarely exceeds 60s. We trust the server hint but never
    # block longer than this.
    llm_rate_limit_max_wait: int = 60
    context_window_threshold_chars: int = 200_000

    # ─── History natural-language filter shared-pool safety ──────────────
    # Deterministic parsing is always enabled. Optional LLM enhancement is a
    # kill-switched shared-pool feature and stays OFF until explicitly enabled.
    history_query_llm_enabled: bool = False
    history_query_llm_daily_limit: int = 20
    history_query_llm_cooldown_seconds: float = 10.0

    # ─── Shared environment model pool safety ───────────────────────
    # BYOK remains the default.  Environment keys are invisible to ordinary
    # teachers unless this explicit kill switch is enabled.  When enabled,
    # every actual provider invocation is charged to an in-process per-owner
    # daily request + estimated-token budget.
    shared_pool_enabled: bool = False
    shared_pool_daily_request_limit: int = 100
    shared_pool_daily_estimated_token_limit: int = 100_000

    # ─── Human-in-the-loop ─────────────────────────────────────────────────────
    confidence_threshold: float = 0.6  # below this, trigger human review

    # ─── Indecisiveness Score (P0 fairness signals) ────────────────────────────
    # Normalized standard deviation of expert/sample scores (std / max_score).
    # When IS exceeds this threshold, the Correction is flagged
    # `requires_human_review=true`. 0.15 means "std ≈ 1.5/10" — empirically near
    # the point where the model genuinely cannot agree with itself.
    is_threshold: float = 0.15

    # Number of independent samples to draw when only one expert is available.
    # Used by multi_expert.run_multi_expert to fan out N parallel skill runs on
    # the same provider so we can compute an Indecisiveness Score even in
    # single-provider deployments. Set to 1 to disable multi-sampling. With
    # ≥ 2 experts this knob is ignored — the experts themselves provide variance.
    #
    # Default 1 (cost-frugal): single-provider deployments do NOT pay the 3×
    # LLM call multiplier by default. Teachers who want IS / Minority Veto
    # signals on a single-provider task can opt in per-task via the upcoming
    # task_setup UI control (see plan: hyssop-paper-jaybird).
    multi_sample_n: int = 1

    # Minority-veto rule: if any expert/sample diverges from the median by more
    # than this fraction of max_score, flag `requires_human_review=true` even
    # when the IS itself is below threshold. Captures "1 expert thinks it's
    # 9/10, another thinks 4/10" cases that an averaged score would hide.
    minority_veto_deviation: float = 0.30

    # ─── Progress reporting ────────────────────────────────────────────────────
    progress_ring_buffer_size: int = 200  # max events kept per job

    # ─── Job recovery & maintenance (Task 4) ───────────────────────────────────
    # Hard wall-clock cap on a single grading job. The periodic maintenance loop
    # marks active jobs older than this (by created_at) as error so a wedged
    # worker cannot hold a concurrency slot forever.
    job_timeout_seconds: int = int(os.getenv("SMARTAI_JOB_TIMEOUT_SECONDS", "3600"))
    # How often the per-worker maintenance loop runs recover_interrupted()/maintain().
    job_maintenance_interval_seconds: int = int(
        os.getenv("SMARTAI_JOB_MAINTENANCE_INTERVAL_SECONDS", "300")
    )
    # Completed/error jobs older than this (by completed_at) are pruned.
    job_history_retention_seconds: int = int(
        os.getenv("SMARTAI_JOB_HISTORY_RETENTION_SECONDS", str(30 * 24 * 3600))
    )

    # ─── Normalized grading-run worker (Task 7) ─────────────────────────────────
    # Stable per-process identity used as the lease owner. Multiple processes
    # each get their own id; the DB lease predicate makes concurrent claims safe.
    grading_worker_id: str = os.getenv(
        "SMARTAI_GRADING_WORKER_ID", f"worker-{os.getpid()}"
    )
    # How long a claimed run lease stays valid before another worker may reclaim.
    grading_lease_seconds: int = int(os.getenv("SMARTAI_GRADING_LEASE_SECONDS", "300"))
    # How often a worker renews its lease while a run is in progress.
    grading_heartbeat_seconds: int = int(os.getenv("SMARTAI_GRADING_HEARTBEAT_SECONDS", "60"))
    # How often the poller looks for queued runs to claim.
    grading_poll_seconds: int = int(os.getenv("SMARTAI_GRADING_POLL_SECONDS", "5"))

    # ─── OCR / vision ingest ───────────────────────────────────────────────────
    ocr_default_provider: Literal["llm_vision", "mathpix"] = "llm_vision"
    ocr_max_pdf_pages: int = 30
    ocr_max_image_bytes: int = 10 * 1024 * 1024
    ocr_render_dpi_scale: float = 2.0
    ocr_concurrency: int = 2
    ocr_text_min_chars: int = 50
    mathpix_app_id: str = os.getenv("MATHPIX_APP_ID", "")
    mathpix_app_key: str = os.getenv("MATHPIX_APP_KEY", "")

    # ─── Frontend ──────────────────────────────────────────────────────────────
    frontend_urls: str = os.getenv(
        "FRONTEND_URLS",
        "http://localhost:8501,http://localhost:3000,http://localhost:8001,"
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    backend_port: int = 8000

    # Persistent application data. SQLite is convenient for local development;
    # deployed environments should set this to a PostgreSQL URL.
    # ON selects the heavier PostgreSQL deployment mode; OFF selects SQLite.
    database_heavy: bool = os.getenv("SMARTAI_DATABASE_HEAVY", "OFF").strip().upper() == "ON"
    # SMARTAI_DATABASE_URL remains a legacy single-URL fallback. Prefer the
    # explicit light/heavy pair so changing only SMARTAI_DATABASE_HEAVY switches
    # the selected database.
    database_url: str = os.getenv("SMARTAI_DATABASE_URL", "")
    database_url_light: str = os.getenv("SMARTAI_DATABASE_URL_LIGHT", "")
    database_url_heavy: str = os.getenv("SMARTAI_DATABASE_URL_HEAVY", "")
    database_auto_create: bool = os.getenv("SMARTAI_DATABASE_AUTO_CREATE", "true").lower() == "true"
    storage_root: str = os.getenv("SMARTAI_STORAGE_ROOT", "data/uploads")
    storage_backend: Literal["local", "object"] = os.getenv("SMARTAI_STORAGE_BACKEND", "local")  # type: ignore[assignment]
    storage_s3_endpoint: Optional[str] = os.getenv("SMARTAI_STORAGE_S3_ENDPOINT", "")
    storage_s3_bucket: Optional[str] = os.getenv("SMARTAI_STORAGE_S3_BUCKET", "")
    storage_s3_region: str = os.getenv("SMARTAI_STORAGE_S3_REGION", "auto")
    storage_s3_access_key: Optional[str] = os.getenv("SMARTAI_STORAGE_S3_ACCESS_KEY", "")
    storage_s3_secret_key: Optional[str] = os.getenv("SMARTAI_STORAGE_S3_SECRET_KEY", "")

    # Stable master key for encrypting user BYOK provider credentials. It must
    # come from the process environment/secret manager and never from source
    # control or the database.
    provider_encryption_key: Optional[str] = os.getenv("SMARTAI_PROVIDER_ENCRYPTION_KEY", "")

    # ─── Auth (JWT) ────────────────────────────────────────────────────────────
    jwt_secret: str = os.getenv("JWT_SECRET", "smartai-dev-secret-change-in-prod")
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 30
    refresh_session_days: int = 30
    refresh_cookie_name: str = "smartai_refresh"
    refresh_cookie_secure: bool = os.getenv("SMARTAI_REFRESH_COOKIE_SECURE", "false").lower() == "true"
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = os.getenv("SMARTAI_REFRESH_COOKIE_SAMESITE", "lax")  # type: ignore[assignment]

    # If true, requests without a valid token are rejected by protected
    # endpoints. If false (dev default), missing tokens are silently mapped
    # to an "anonymous" user so the legacy non-auth flow still works.
    require_auth: bool = os.getenv("SMARTAI_REQUIRE_AUTH", "false").lower() == "true"

    # Synthetic demo tokens are forgeable and must only be enabled explicitly
    # for local/E2E workflows. Real JWT authentication remains the default.
    allow_demo_tokens: bool = os.getenv("SMARTAI_ALLOW_DEMO_TOKENS", "false").lower() == "true"
    e2e_fake_provider: bool = os.getenv("SMARTAI_E2E_FAKE_PROVIDER", "false").lower() == "true"
    e2e_fail_qid: str = os.getenv("SMARTAI_E2E_FAIL_QID", "")

    # If true, /auth/register is closed; requests get a 403 with "registration
    # closed" message. Demo accounts are seeded from `test_users_file` instead.
    registration_closed: bool = os.getenv("SMARTAI_REGISTRATION_CLOSED", "true").lower() == "true"

    # Path to a JSON file containing pre-seeded test accounts.
    # Format: {"users": [{"username": "...", "password": "...", "role": "teacher"}, ...]}
    # The file MUST be gitignored — keep credentials out of the repo. Generate
    # via `python scripts/generate_test_users.py` (creates 50 random accounts).
    test_users_file: str = os.getenv("SMARTAI_TEST_USERS_FILE", "data/test_users.json")
    seed_test_users: bool = os.getenv("SMARTAI_SEED_TEST_USERS", "true").lower() == "true"

    @model_validator(mode="after")
    def resolve_database_url(self) -> "Settings":
        process_legacy_url = os.getenv("SMARTAI_DATABASE_URL", "").strip()
        process_light_url = os.getenv("SMARTAI_DATABASE_URL_LIGHT", "").strip()
        process_heavy_url = os.getenv("SMARTAI_DATABASE_URL_HEAVY", "").strip()
        legacy_url = process_legacy_url or self.database_url.strip()
        light_url = self.database_url_light.strip()
        heavy_url = self.database_url_heavy.strip()
        if process_legacy_url and not (process_light_url or process_heavy_url):
            light_url = process_legacy_url if process_legacy_url.startswith("sqlite") else light_url
            heavy_url = process_legacy_url if process_legacy_url.startswith(("postgresql://", "postgresql+")) else heavy_url
        if not light_url:
            light_url = legacy_url if legacy_url.startswith("sqlite") else "sqlite:///data/smartai.db"
        if not heavy_url and legacy_url.startswith(("postgresql://", "postgresql+")):
            heavy_url = legacy_url
        self.database_url = heavy_url if self.database_heavy else light_url
        return self

    model_config = {"env_prefix": "SMARTAI_", "env_file": ".env", "extra": "ignore"}


# Global singleton
settings = Settings()
