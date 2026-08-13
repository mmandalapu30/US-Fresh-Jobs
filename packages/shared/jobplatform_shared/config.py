"""Central typed settings.

Every knob the platform has lives here and comes from the environment. Nothing reads
``os.environ`` directly anywhere else, so there is exactly one place to audit for
configuration and exactly one place that knows what a secret is.
"""

from __future__ import annotations

import functools
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "test", "production"]

#: Placeholder token used throughout the tracked ``.env.*.example`` templates. Production
#: startup fails if any secret still carries it — this is what stops a template value from
#: silently reaching a live deployment.
PLACEHOLDER_SENTINEL = "CHANGE_ME"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,  # the process environment is the source of truth; compose/CI inject it
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- runtime ----------------------------------------------------------------
    environment: Environment = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ---- database ---------------------------------------------------------------
    database_url: PostgresDsn
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_pool_timeout: int = Field(default=30, ge=1)
    database_statement_timeout_ms: int = Field(default=15_000, ge=100)

    # ---- redis ------------------------------------------------------------------
    redis_url: RedisDsn
    celery_broker_url: str
    celery_result_backend: str

    # ---- object storage ---------------------------------------------------------
    object_storage_url: str
    object_storage_bucket: str
    object_storage_access_key: SecretStr
    object_storage_secret_key: SecretStr
    object_storage_region: str = "us-east-1"
    object_storage_use_ssl: bool = False

    # ---- security ---------------------------------------------------------------
    secret_key: SecretStr
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = Field(default=15, ge=1, le=1440)
    jwt_refresh_token_expire_days: int = Field(default=30, ge=1, le=365)
    # NoDecode: pydantic-settings would otherwise try json.loads() on the raw env
    # string before any validator runs, so a plain CSV value would be a hard error.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---- rate limiting ----------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_anonymous: int = Field(default=60, ge=1)
    rate_limit_authenticated: int = Field(default=300, ge=1)
    rate_limit_admin: int = Field(default=1000, ge=1)

    # ---- api --------------------------------------------------------------------
    # Binding all interfaces is required inside a container: the process must accept
    # connections from the Docker network, and exposure is controlled by the published
    # port and the reverse proxy, not by this bind address.
    api_host: str = "0.0.0.0"  # noqa: S104
    api_port: int = 8000
    api_v1_prefix: str = "/api/v1"
    api_default_page_size: int = Field(default=25, ge=1, le=200)
    api_max_page_size: int = Field(default=100, ge=1, le=500)

    # ---- source: openjobdata ----------------------------------------------------
    # See docs/00-source-verification.md. These are configuration, not assumptions:
    # anything unverified about the source is expressed as a setting rather than
    # hard-coded into the connector.
    openjobdata_enabled: bool = True
    openjobdata_bucket_uri: str = "hf://buckets/Invicto69/Jobs-Dataset-bucket"
    openjobdata_variant: Literal["full", "minimal"] = "full"
    openjobdata_include_entire_json: bool = False
    openjobdata_max_concurrency: int = Field(default=2, ge=1, le=16)
    openjobdata_request_timeout_seconds: int = Field(default=300, ge=10)
    openjobdata_retry_attempts: int = Field(default=5, ge=1, le=20)
    huggingface_token: SecretStr | None = None

    # ---- ingestion policy -------------------------------------------------------
    ingest_country_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["US"])
    #: Role categories to ingest. EMPTY MEANS ALL -- the platform's default is to keep
    #: every qualifying job, per the "no artificial limits" requirement. Narrowing this is
    #: an explicit operator decision, and skipped rows are still recorded with a reason so
    #: the cost of the choice is visible rather than silent.
    ingest_category_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Categories to exclude. Applied after the allowlist.
    ingest_category_blocklist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Only ingest jobs posted within this many days. 0 disables the window.
    #: The source's daily deltas are dominated by re-observations of old postings --
    #: measured: only 8% of accepted rows were posted within 7 days -- so without this
    #: the feed fills with months-old jobs.
    ingest_max_posted_age_days: int = Field(default=0, ge=0, le=3650)
    #: When a job has no posted_at (17% of rows), fall back to our own detection time
    #: rather than dropping it. A job we first saw today is fresh to our users even if
    #: the employer never stated a date; the alternative is discarding a sixth of the feed.
    ingest_age_fallback_to_first_seen: bool = True
    #: Delete jobs older than this many days. 0 disables retention entirely.
    #: Runs via scripts/enforce_retention.py (schedulable).
    retention_max_posted_age_days: int = Field(default=0, ge=0, le=3650)
    ingest_row_group_batch_size: int = Field(default=8, ge=1, le=256)
    ingest_upsert_chunk_size: int = Field(default=5_000, ge=100, le=100_000)
    ingest_max_future_posted_at_hours: int = Field(default=24, ge=0, le=8760)
    ingest_min_posted_at_year: int = Field(default=2000, ge=1970, le=2100)
    lifecycle_removed_after_days: int = Field(default=14, ge=1, le=365)

    # ---- observability ----------------------------------------------------------
    metrics_enabled: bool = True
    sentry_dsn: str | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # ---- validators -------------------------------------------------------------

    @field_validator(
        "cors_allow_origins",
        "ingest_country_allowlist",
        "ingest_category_allowlist",
        "ingest_category_blocklist",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept ``a,b,c`` from env as a list. Env vars cannot carry JSON comfortably."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {v!r}")
        return upper

    @field_validator("ingest_country_allowlist")
    @classmethod
    def _upper_country_codes(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v]

    @field_validator("ingest_category_allowlist", "ingest_category_blocklist")
    @classmethod
    def _lower_category_slugs(cls, v: list[str]) -> list[str]:
        return [c.strip().lower() for c in v if c.strip()]

    @model_validator(mode="after")
    def _production_hardening(self) -> Settings:
        """Fail fast rather than run a production deploy with template values.

        A misconfigured production process that boots is far more dangerous than one that
        refuses to. Each check below corresponds to a real, exploitable misconfiguration.
        """
        if self.environment != "production":
            return self

        problems: list[str] = []

        secrets_to_check = {
            "SECRET_KEY": self.secret_key,
            "JWT_SECRET": self.jwt_secret,
            "OBJECT_STORAGE_ACCESS_KEY": self.object_storage_access_key,
            "OBJECT_STORAGE_SECRET_KEY": self.object_storage_secret_key,
        }
        for name, secret in secrets_to_check.items():
            value = secret.get_secret_value()
            if PLACEHOLDER_SENTINEL in value:
                problems.append(f"{name} still contains the {PLACEHOLDER_SENTINEL} placeholder")
            elif len(value) < 32:
                problems.append(f"{name} is shorter than 32 characters")

        if self.secret_key.get_secret_value() == self.jwt_secret.get_secret_value():
            problems.append("SECRET_KEY and JWT_SECRET must not be identical")

        if self.debug:
            problems.append("DEBUG must be false in production")

        if "*" in self.cors_allow_origins:
            problems.append("CORS_ALLOW_ORIGINS must not be '*' in production")

        if not self.cors_allow_origins:
            problems.append("CORS_ALLOW_ORIGINS must be set in production")

        for origin in self.cors_allow_origins:
            if origin.startswith("http://"):
                problems.append(f"CORS origin {origin!r} must use https in production")

        if not self.rate_limit_enabled:
            problems.append("RATE_LIMIT_ENABLED must be true in production")

        if problems:
            raise ValueError(
                "Refusing to start in production with an unsafe configuration:\n  - "
                + "\n  - ".join(problems)
            )
        return self

    # ---- derived helpers --------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def sync_database_url(self) -> str:
        """psycopg3 sync DSN, used by Alembic and the ingestion workers."""
        return str(self.database_url)

    def safe_dump(self) -> dict[str, object]:
        """Config snapshot with every secret redacted — safe for logs and /health output."""
        data = self.model_dump()
        for key, value in list(data.items()):
            if isinstance(getattr(self, key, None), SecretStr):
                data[key] = "***REDACTED***" if value else None
        # the DSN embeds a password
        data["database_url"] = _redact_dsn(str(self.database_url))
        data["redis_url"] = _redact_dsn(str(self.redis_url))
        data["celery_broker_url"] = _redact_dsn(self.celery_broker_url)
        data["celery_result_backend"] = _redact_dsn(self.celery_result_backend)
        return data


def _redact_dsn(dsn: str) -> str:
    """Strip credentials out of a URL so it can be logged."""
    if "://" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    if "@" not in rest:
        return dsn
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so validation runs once per process.

    Call ``get_settings.cache_clear()`` in tests that mutate the environment.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
