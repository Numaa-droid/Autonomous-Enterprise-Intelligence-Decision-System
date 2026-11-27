"""Settings from environment variables (prefix ``AEIDS_``) or ``.env``. No secrets in code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_QUERY_INSTRUCTION = (
    "Given a question about a company's annual report or financial filing, "
    "retrieve the passages and tables that answer it"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEIDS_", env_file=".env", extra="ignore")

    database_url: str = Field(
        default="sqlite:///./aeids.db",
        description="SQLAlchemy URL; Postgres in normal use, e.g. postgresql+psycopg://...",
        repr=False,  # it holds the database password: never shown when settings are printed (D-034)
    )
    # Host names the API answers to; anything else is refused (DNS rebinding, D-034)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "::1"], min_length=1)
    storage_root: Path = Path("./storage")
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    # TableLens _structure.json files grow to tens of MB on long reports
    max_structure_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    default_tenant_name: str = "default"

    # --- OpenRouter: embeddings and LLM (D-001, D-002) ---
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "qwen/qwen3-embedding-8b"
    # Qwen3 embeddings expect an instruction on queries only (documents are embedded as is)
    embedding_query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    embedding_batch_size: int = Field(default=32, ge=1, le=512)
    embedding_concurrency: int = Field(default=4, ge=1, le=16)  # batches in flight
    # OpenRouter serves this model from several providers whose vectors differ (bf16 / fp8 ...);
    # pin one so documents and questions are embedded the same way (D-023). Empty: any provider.
    embedding_provider: str | None = "nebius"
    embedding_allow_fallbacks: bool = False
    # question vectors are cached in Postgres: the same question always gets the same vector
    query_embedding_cache: bool = True
    llm_model: str = "qwen/qwen3.8-flash"
    # hidden "thinking" before a reply: off makes agent JSON calls 2-3x faster with the same
    # answers, and avoids empty replies (the budget spent on thinking); D-030
    llm_reasoning: Literal["off", "low", "medium", "high", "model_default"] = "off"
    llm_timeout_seconds: float = Field(default=45.0, gt=0)  # per call; a stuck reply fails fast and is retried
    llm_max_retries: int = Field(default=3, ge=0, le=6)  # transient errors only (rule 8)
    llm_backoff_seconds: float = Field(default=1.0, ge=0)

    # --- services ---
    qdrant_url: str = "http://127.0.0.1:6333"
    # hosted Qdrant (Qdrant Cloud) only; never printed (D-037)
    qdrant_api_key: SecretStr | None = Field(default=None, repr=False)
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- TableLens (tablelens/app.py) parses uploaded PDFs (D-019) ---
    tablelens_url: str = "http://127.0.0.1:8000"
    tablelens_poll_seconds: float = Field(default=3.0, gt=0, le=10)
    tablelens_job_timeout_seconds: float = Field(default=4 * 3600, gt=0)
    # queue: uploads start a background job (RQ worker: python -m aeids.worker)
    # inline: run in the request (tests, debugging) · off: nothing starts automatically
    jobs_mode: Literal["queue", "inline", "off"] = "queue"

    # --- workflows (M3, D-024) ---
    task_max_attempts: int = Field(default=3, ge=1, le=10)
    task_timeout_seconds: int = Field(default=180, ge=5, le=3600)  # also the worker's lease
    task_retry_backoff_seconds: float = Field(default=5.0, ge=0, le=600)  # doubled per attempt
    workflow_max_rework_rounds: int = Field(default=2, ge=0, le=5)  # MVP_PLAN M5: at most 2
    stub_agent_delay_seconds: float = Field(default=0.0, ge=0, le=120)  # demo / crash tests
    # M4: "llm" runs the LLM agents, "stub" the M3 stand-ins, "auto" the LLM agents when an
    # OpenRouter key is set (D-025)
    agent_mode: Literal["auto", "llm", "stub"] = "auto"
    # Document Intelligence: code picks a table cell when the match is clear and the model only
    # the rest (code_first), or the model picks every cell (model); D-030
    di_cell_resolver: Literal["code_first", "model"] = "code_first"

    # --- run tracing (D-022): spans stored in Postgres, shown in the dashboard ---
    tracing_enabled: bool = True
    # where ./start.sh writes the logs of TableLens, the API and the worker (dashboard: Logs)
    log_dir: Path = Path("./logs")


@lru_cache
def get_settings() -> Settings:
    return Settings()
