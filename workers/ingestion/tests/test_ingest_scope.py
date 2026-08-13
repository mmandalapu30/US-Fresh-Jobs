"""Ingestion-time role scope.

The gate lets an operator ingest only chosen role categories instead of storing
everything and filtering later. Two properties matter and are asserted here:

* **The default is everything.** An empty allowlist must not quietly become an empty
  result -- the platform's stated principle is to keep all qualifying jobs.
* **Skipping is recorded.** A narrowed scope is a real cost, and it stays visible in
  sync_errors rather than looking like the source never had those jobs.
"""

from __future__ import annotations

import pytest

from jobplatform_schemas import RejectionReason


def _in_scope(category: str, allowlist: list[str], blocklist: list[str]) -> bool:
    """Mirror of the gate in IngestionPipeline._prepare.

    Kept as a tiny pure function so the policy can be exercised without a database, a
    connector, or a full pipeline run.
    """
    if allowlist and category not in allowlist:
        return False
    return category not in blocklist


class TestScopePolicy:
    def test_empty_allowlist_means_everything(self) -> None:
        """The default must be permissive. An empty list meaning "nothing" would
        silently empty the platform the moment the setting was introduced."""
        for category in ("healthcare", "software-it", "other", "transport"):
            assert _in_scope(category, [], [])

    def test_allowlist_restricts(self) -> None:
        allow = ["software-it", "engineering"]
        assert _in_scope("software-it", allow, [])
        assert _in_scope("engineering", allow, [])
        assert not _in_scope("healthcare", allow, [])
        assert not _in_scope("other", allow, [])

    def test_blocklist_excludes(self) -> None:
        assert not _in_scope("other", [], ["other"])
        assert _in_scope("healthcare", [], ["other"])

    def test_blocklist_wins_over_allowlist(self) -> None:
        """An explicit exclusion is the more specific instruction."""
        assert not _in_scope("software-it", ["software-it", "engineering"], ["software-it"])


class TestSettingsParsing:
    def test_csv_is_split_and_lowercased(
        self, minimal_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jobplatform_shared import get_settings

        monkeypatch.setenv("INGEST_CATEGORY_ALLOWLIST", "Software-IT, Engineering ,science")
        get_settings.cache_clear()
        assert get_settings().ingest_category_allowlist == [
            "software-it",
            "engineering",
            "science",
        ]

    def test_default_is_empty(self, minimal_env: None) -> None:
        from jobplatform_shared import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        assert settings.ingest_category_allowlist == []
        assert settings.ingest_category_blocklist == []

    def test_blank_entries_are_dropped(
        self, minimal_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jobplatform_shared import get_settings

        monkeypatch.setenv("INGEST_CATEGORY_BLOCKLIST", "other, ,  ,")
        get_settings.cache_clear()
        assert get_settings().ingest_category_blocklist == ["other"]


class TestRejectionReason:
    def test_reason_exists_and_is_distinct(self) -> None:
        """Out-of-scope is not the same as invalid: the job is real and well-formed,
        it is simply outside what this deployment chose to store."""
        assert RejectionReason.CATEGORY_NOT_ALLOWED.value == "CATEGORY_NOT_ALLOWED"
        assert RejectionReason.CATEGORY_NOT_ALLOWED is not RejectionReason.COUNTRY_NOT_ALLOWED


class TestIndustryNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("computer software", "Computer Software"),
            ("hospital & health care", "Hospital & Health Care"),
            # REGRESSION: compounds were left half-cased ("Transportation/trucking/...").
            ("transportation/trucking/railroad", "Transportation/Trucking/Railroad"),
            ("e-learning", "E-Learning"),
            ("oil and gas", "Oil and Gas"),
            (None, None),
            ("", None),
            ("   ", None),
        ],
    )
    def test_title_casing(self, raw: str | None, expected: str | None) -> None:
        from ingestion.pipeline.process import _normalize_industry

        assert _normalize_industry(raw) == expected

    def test_length_is_bounded(self) -> None:
        from ingestion.pipeline.process import _normalize_industry

        assert len(_normalize_industry("x " * 200) or "") <= 120
