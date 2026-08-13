"""Settings validation, with emphasis on the production safety gate.

The production hardening rules are security controls, not style preferences: each test
here corresponds to a misconfiguration that has taken real systems down or exposed them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from jobplatform_shared.config import PLACEHOLDER_SENTINEL, Settings, _redact_dsn, get_settings


class TestBasicSettings:
    def test_loads_from_environment(self, minimal_env: None) -> None:
        settings = get_settings()
        assert settings.environment == "development"
        assert settings.openjobdata_variant == "full"

    def test_variant_defaults_to_full(self, minimal_env: None) -> None:
        """'minimal' lacks city/state/salary/description (source doc §5), so 'full' is
        the only default that can satisfy the product requirements."""
        assert get_settings().openjobdata_variant == "full"

    def test_entire_json_excluded_by_default(self, minimal_env: None) -> None:
        """Including entire_json would double the daily download for data we never read."""
        assert get_settings().openjobdata_include_entire_json is False

    def test_csv_lists_are_split(self, minimal_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://a.com, http://b.com")
        monkeypatch.setenv("INGEST_COUNTRY_ALLOWLIST", "us, ca")
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.cors_allow_origins == ["http://a.com", "http://b.com"]
        assert settings.ingest_country_allowlist == ["US", "CA"]  # upper-cased

    def test_invalid_log_level_rejected(
        self, minimal_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "CHATTY")
        get_settings.cache_clear()
        with pytest.raises(PydanticValidationError, match="log_level"):
            get_settings()

    def test_settings_are_cached(self, minimal_env: None) -> None:
        assert get_settings() is get_settings()


class TestSecretHandling:
    def test_secrets_are_not_in_repr(self, minimal_env: None) -> None:
        """A settings object reaching a log or traceback must not leak its secrets."""
        settings = get_settings()
        assert "s" * 64 not in repr(settings)
        assert "j" * 64 not in repr(settings)

    def test_safe_dump_redacts_every_secret(self, minimal_env: None) -> None:
        dumped = get_settings().safe_dump()
        assert dumped["secret_key"] == "***REDACTED***"
        assert dumped["jwt_secret"] == "***REDACTED***"
        assert dumped["object_storage_secret_key"] == "***REDACTED***"

    def test_safe_dump_redacts_dsn_password(self, minimal_env: None) -> None:
        """The password lives inside the DSN string, so field-level redaction is not enough."""
        dumped = get_settings().safe_dump()
        assert "p@" not in str(dumped["database_url"])
        assert "***" in str(dumped["database_url"])

    @pytest.mark.parametrize(
        ("dsn", "expected"),
        [
            ("postgresql://user:hunter2@host:5432/db", "postgresql://user:***@host:5432/db"),
            ("redis://localhost:6379/0", "redis://localhost:6379/0"),  # no credentials
            ("memory://", "memory://"),
        ],
    )
    def test_redact_dsn(self, dsn: str, expected: str) -> None:
        assert _redact_dsn(dsn) == expected


class TestProductionHardening:
    """Each case here is a real deployment mistake the config layer must refuse."""

    @staticmethod
    def _prod_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
        base = {
            "ENVIRONMENT": "production",
            "DEBUG": "false",
            "DATABASE_URL": "postgresql+psycopg://u:p@db:5432/prod",
            "REDIS_URL": "redis://cache:6379/0",
            "CELERY_BROKER_URL": "redis://cache:6379/1",
            "CELERY_RESULT_BACKEND": "redis://cache:6379/2",
            "OBJECT_STORAGE_URL": "https://s3.amazonaws.com",
            "OBJECT_STORAGE_BUCKET": "prod",
            "OBJECT_STORAGE_ACCESS_KEY": "A" * 40,
            "OBJECT_STORAGE_SECRET_KEY": "B" * 40,
            "SECRET_KEY": "S" * 64,
            "JWT_SECRET": "J" * 64,
            "CORS_ALLOW_ORIGINS": "https://jobs.example.com",
            "RATE_LIMIT_ENABLED": "true",
        }
        base.update(overrides)
        for key, value in base.items():
            monkeypatch.setenv(key, value)

    def test_valid_production_config_starts(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prod_env(monkeypatch)
        settings = Settings()  # type: ignore[call-arg]
        assert settings.is_production

    def test_placeholder_secret_refused(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The single most dangerous case: a template value shipped to production."""
        self._prod_env(
            monkeypatch, SECRET_KEY=f"{PLACEHOLDER_SENTINEL}_xxxxxxxxxxxxxxxxxxxxxxxxxxx"
        )
        with pytest.raises(PydanticValidationError, match="placeholder"):
            Settings()  # type: ignore[call-arg]

    def test_short_secret_refused(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prod_env(monkeypatch, JWT_SECRET="short")
        with pytest.raises(PydanticValidationError, match="shorter than 32"):
            Settings()  # type: ignore[call-arg]

    def test_reused_secret_refused(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sharing one key between session signing and JWTs lets one leak break both."""
        same = "K" * 64
        self._prod_env(monkeypatch, SECRET_KEY=same, JWT_SECRET=same)
        with pytest.raises(PydanticValidationError, match="must not be identical"):
            Settings()  # type: ignore[call-arg]

    def test_debug_refused(self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prod_env(monkeypatch, DEBUG="true")
        with pytest.raises(PydanticValidationError, match="DEBUG must be false"):
            Settings()  # type: ignore[call-arg]

    def test_wildcard_cors_refused(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prod_env(monkeypatch, CORS_ALLOW_ORIGINS="*")
        with pytest.raises(PydanticValidationError, match=r"must not be '\*'"):
            Settings()  # type: ignore[call-arg]

    def test_plaintext_cors_origin_refused(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prod_env(monkeypatch, CORS_ALLOW_ORIGINS="http://jobs.example.com")
        with pytest.raises(PydanticValidationError, match="must use https"):
            Settings()  # type: ignore[call-arg]

    def test_disabled_rate_limiting_refused(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prod_env(monkeypatch, RATE_LIMIT_ENABLED="false")
        with pytest.raises(PydanticValidationError, match="RATE_LIMIT_ENABLED"):
            Settings()  # type: ignore[call-arg]

    def test_development_is_not_hardened(
        self, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate must not make local development painful."""
        self._prod_env(
            monkeypatch,
            ENVIRONMENT="development",
            DEBUG="true",
            CORS_ALLOW_ORIGINS="*",
            SECRET_KEY="short",
            JWT_SECRET="short",
        )
        settings = Settings()  # type: ignore[call-arg]
        assert settings.environment == "development"
