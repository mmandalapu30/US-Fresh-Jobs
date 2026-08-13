"""Pipeline service tests: location, freshness, deduplication.

These encode behaviour that real ingestion runs proved necessary. Several cases exist
because a naive implementation shipped a visible bug first — the Canadian-jobs-in-a-US-feed
regression in particular.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ingestion.services import DedupeService, FreshnessService, LocationNormalizer
from jobplatform_schemas import FreshnessBucket, JobStatus, RejectionReason, RemoteType

# ---------------------------------------------------------------------------- location


class TestUSClassification:
    @pytest.fixture
    def normalizer(self) -> LocationNormalizer:
        return LocationNormalizer()

    @pytest.mark.parametrize(
        ("kwargs", "state"),
        [
            (
                {"raw_country": "United States", "raw_state": "Michigan", "raw_city": "Detroit"},
                "MI",
            ),
            ({"raw_country": "United States", "raw_state": "OH", "raw_city": "Fairborn"}, "OH"),
            ({"raw_location_text": "Fairborn, OH 45324"}, "OH"),
            ({"raw_location_text": "            South Carolina"}, "SC"),
            ({"raw_country": "United States", "raw_city": "NYC"}, "NY"),
        ],
    )
    def test_us_jobs_are_recognised(
        self, normalizer: LocationNormalizer, kwargs: dict, state: str
    ) -> None:
        result = normalizer.normalize(**kwargs)
        assert result.is_us
        assert result.state_code == state

    @pytest.mark.parametrize("text", ["US Remote", "Remote - United States", "Remote (US)"])
    def test_remote_us_phrases(self, normalizer: LocationNormalizer, text: str) -> None:
        assert normalizer.normalize(raw_location_text=text).is_us


class TestForeignRejection:
    """Each case here shipped as a bug at least once, or would have."""

    @pytest.fixture
    def normalizer(self) -> LocationNormalizer:
        return LocationNormalizer()

    @pytest.mark.parametrize("state", ["Quebec", "Ontario", "Maharashtra", "New South Wales"])
    def test_foreign_state_overrides_a_us_country_claim(
        self, normalizer: LocationNormalizer, state: str
    ) -> None:
        """The source really does pair country='United States' with a foreign state."""
        result = normalizer.normalize(raw_country="United States", raw_state=state)
        assert not result.is_us
        assert "not a US subdivision" in (result.reason or "")

    @pytest.mark.parametrize("city", ["Toronto", "Montreal", "Mississauga", "Bengaluru", "Sydney"])
    def test_foreign_city_overrides_a_us_country_claim(
        self, normalizer: LocationNormalizer, city: str
    ) -> None:
        """REGRESSION: 317 Toronto and 155 Montreal jobs reached a US-only feed labelled
        country='United States'. A definitively foreign city now outranks that field."""
        result = normalizer.normalize(raw_country="United States", raw_city=city)
        assert not result.is_us
        assert "not a US city" in (result.reason or "")

    @pytest.mark.parametrize("city", ["Ottawa", "Vancouver", "London", "Cambridge"])
    def test_ambiguous_city_without_a_state_is_not_confirmed_us(
        self, normalizer: LocationNormalizer, city: str
    ) -> None:
        """Ottawa ON and Ottawa IL are indistinguishable without a state. Withholding an
        unconfirmable job beats showing a foreign one in a US-only product."""
        assert not normalizer.normalize(raw_country="United States", raw_city=city).is_us

    @pytest.mark.parametrize(
        ("city", "state"), [("Ottawa", "OH"), ("Vancouver", "WA"), ("Cambridge", "MA")]
    )
    def test_ambiguous_city_with_a_state_is_accepted(
        self, normalizer: LocationNormalizer, city: str, state: str
    ) -> None:
        """The guard must not throw away real US cities that happen to share a name."""
        result = normalizer.normalize(raw_country="United States", raw_city=city, raw_state=state)
        assert result.is_us
        assert result.state_code == state

    def test_city_alone_never_implies_a_country(self, normalizer: LocationNormalizer) -> None:
        assert not normalizer.normalize(raw_city="London").is_us

    @pytest.mark.parametrize("junk", ["", "REMOTE", "tbc", None])
    def test_junk_country_values(self, normalizer: LocationNormalizer, junk: str | None) -> None:
        assert normalizer.normalize(raw_country=junk).country_code is None


class TestLocationDetails:
    @pytest.fixture
    def normalizer(self) -> LocationNormalizer:
        return LocationNormalizer()

    def test_state_code_in_the_city_field_is_promoted(self, normalizer: LocationNormalizer) -> None:
        """REGRESSION: the source ships city='TX'. Left alone it produced jobs whose city
        was a state code and whose state was null."""
        result = normalizer.normalize(raw_country="United States", raw_city="TX")
        assert result.state_code == "TX"
        assert result.city is None

    def test_full_state_names_in_the_city_field_are_left_alone(
        self, normalizer: LocationNormalizer
    ) -> None:
        """Wyoming MI and Delaware OH are real cities; promoting them would corrupt them."""
        result = normalizer.normalize(
            raw_country="United States", raw_city="Wyoming", raw_state="MI"
        )
        assert result.city == "Wyoming"
        assert result.state_code == "MI"

    def test_padding_is_stripped(self, normalizer: LocationNormalizer) -> None:
        result = normalizer.normalize(raw_country="United States", raw_city="  Detroit  ")
        assert result.city == "Detroit"

    def test_city_key_collapses_punctuation(self, normalizer: LocationNormalizer) -> None:
        """ "St. Louis" and "St Louis" must not fragment the location dimension."""
        a = normalizer.normalize(raw_country="United States", raw_city="St. Louis", raw_state="MO")
        b = normalizer.normalize(raw_country="United States", raw_city="St Louis", raw_state="MO")
        assert a.city_normalized == b.city_normalized

    def test_zip_is_only_kept_for_us_jobs(self, normalizer: LocationNormalizer) -> None:
        assert (
            normalizer.normalize(raw_country="Canada", raw_postal_code="K1A0B1").postal_code is None
        )

    @pytest.mark.parametrize(
        ("workplace", "expected"),
        [
            ("remote", RemoteType.REMOTE),
            ("hybrid", RemoteType.HYBRID),
            ("onsite", RemoteType.ONSITE),
            (None, RemoteType.UNKNOWN),
        ],
    )
    def test_remote_type(
        self, normalizer: LocationNormalizer, workplace: str | None, expected: RemoteType
    ) -> None:
        result = normalizer.normalize(raw_country="United States", raw_workplace_type=workplace)
        assert result.remote_type is expected

    def test_unknown_workplace_is_not_assumed_onsite(self, normalizer: LocationNormalizer) -> None:
        """The source's 'tbc' is ~41% of rows; calling it on-site would misfile all of them."""
        assert normalizer.normalize(raw_country="United States").remote_type is RemoteType.UNKNOWN


# --------------------------------------------------------------------------- freshness


class TestFreshness:
    @pytest.fixture
    def service(self) -> FreshnessService:
        return FreshnessService(max_future_hours=24, min_year=2000)

    def test_a_normal_date_is_valid(self, service: FreshnessService) -> None:
        assert service.assess(datetime.now(UTC) - timedelta(hours=2)).is_valid

    def test_missing_posted_at_is_not_an_error(self, service: FreshnessService) -> None:
        """19.3% of source rows have none. Rejecting them would discard a fifth of the
        dataset; they are kept and simply excluded from 'posted recently'."""
        assessment = service.assess(None)
        assert not assessment.is_valid
        assert assessment.rejection_reason is None

    def test_future_dates_are_flagged_not_clamped(self, service: FreshnessService) -> None:
        """The source publishes dates up to 34 days ahead. Clamping would fabricate a
        timestamp, which the spec forbids."""
        future = datetime.now(UTC) + timedelta(days=34)
        assessment = service.assess(future)
        assert not assessment.is_valid
        assert assessment.rejection_reason is RejectionReason.FUTURE_POSTED_AT
        assert assessment.posted_at == future  # preserved exactly

    def test_small_future_skew_is_tolerated(self, service: FreshnessService) -> None:
        """A source publishing near midnight in a local zone can look slightly ahead."""
        assert service.assess(datetime.now(UTC) + timedelta(hours=6)).is_valid

    def test_implausibly_old_dates_are_flagged(self, service: FreshnessService) -> None:
        assessment = service.assess(datetime(1995, 1, 1, tzinfo=UTC))
        assert assessment.rejection_reason is RejectionReason.IMPLAUSIBLE_POSTED_AT

    def test_detection_beats_posting_for_bucketing(self, service: FreshnessService) -> None:
        """Our own clock is the strongest freshness signal we can honestly offer."""
        now = datetime.now(UTC)
        bucket = service.classify(
            posted_at=now - timedelta(days=30),
            posted_at_is_valid=True,
            first_seen_at=now - timedelta(minutes=10),
            now=now,
        )
        assert bucket is FreshnessBucket.NEW_LAST_HOUR

    def test_untrusted_posted_at_never_drives_a_posted_bucket(
        self, service: FreshnessService
    ) -> None:
        now = datetime.now(UTC)
        bucket = service.classify(
            posted_at=now + timedelta(days=30),
            posted_at_is_valid=False,
            first_seen_at=now - timedelta(days=5),
            now=now,
        )
        assert bucket is not FreshnessBucket.POSTED_TODAY

    def test_closed_jobs_bucket_as_expired(self, service: FreshnessService) -> None:
        now = datetime.now(UTC)
        assert (
            service.classify(
                posted_at=now,
                posted_at_is_valid=True,
                first_seen_at=now,
                status=JobStatus.EXPIRED,
                now=now,
            )
            is FreshnessBucket.EXPIRED
        )


# ---------------------------------------------------------------------------- dedupe


class TestDeduplication:
    @pytest.fixture
    def service(self) -> DedupeService:
        return DedupeService()

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("https://ex.com/job/1?utm_source=x", "https://ex.com/job/1"),
            ("https://WWW.Ex.com/job/1", "https://ex.com/job/1"),
            ("https://ex.com/job/1/", "https://ex.com/job/1"),
            ("https://ex.com/job/1?b=2&a=1", "https://ex.com/job/1?a=1&b=2"),
            ("https://ex.com/job/1#section", "https://ex.com/job/1"),
        ],
    )
    def test_urls_that_mean_the_same_job(self, service: DedupeService, a: str, b: str) -> None:
        assert service.canonicalize_url(a) == service.canonicalize_url(b)

    def test_path_case_is_preserved(self, service: DedupeService) -> None:
        """Many ATS job ids are case-sensitive; lowercasing would merge different jobs."""
        assert service.canonicalize_url("https://ex.com/job/AbC") != service.canonicalize_url(
            "https://ex.com/job/abc"
        )

    @pytest.mark.parametrize("url", [None, "", "javascript:alert(1)", "not a url", "ftp://x/y"])
    def test_non_http_urls_are_refused(self, service: DedupeService, url: str | None) -> None:
        assert service.canonicalize_url(url) is None

    def test_company_legal_suffixes_collapse(self, service: DedupeService) -> None:
        assert service.normalize_company("ABC Corp.") == service.normalize_company(
            "ABC Corporation"
        )

    def test_seniority_is_stripped_from_titles(self, service: DedupeService) -> None:
        assert service.normalize_title("Senior Software Engineer") == service.normalize_title(
            "Software Engineer"
        )

    def test_identical_jobs_share_a_fingerprint(self, service: DedupeService) -> None:
        args = {
            "source": "s",
            "title": "Engineer",
            "company_name": "ABC Inc",
            "company_external_id": "1",
            "apply_url": "https://ex.com/1",
            "country_code": "US",
            "state_code": "MI",
            "city": "Detroit",
        }
        a = service.compute(external_id="a", **args)
        b = service.compute(external_id="b", **args)
        assert a.content_fingerprint == b.content_fingerprint

    def test_different_cities_do_not_collide(self, service: DedupeService) -> None:
        base = {
            "source": "s",
            "external_id": "x",
            "title": "Engineer",
            "company_name": "ABC",
            "company_external_id": "1",
            "apply_url": None,
            "country_code": "US",
            "state_code": "MI",
        }
        assert (
            service.compute(**base, city="Detroit").content_fingerprint
            != service.compute(**base, city="Lansing").content_fingerprint
        )

    def test_generic_titles_disable_level_3(self, service: DedupeService) -> None:
        """ "Cashier" at one company in one city is routinely several distinct openings.
        Merging them would silently delete real jobs."""
        keys = service.compute(
            source="s",
            external_id="x",
            title="Cashier",
            company_name="BigCo",
            company_external_id="1",
            apply_url=None,
            country_code="US",
            state_code="MI",
            city="Detroit",
        )
        assert keys.company_title_location_hash is None

    def test_level_3_requires_a_specific_location(self, service: DedupeService) -> None:
        keys = service.compute(
            source="s",
            external_id="x",
            title="Quantum Cartographer",
            company_name="BigCo",
            company_external_id="1",
            apply_url=None,
            country_code="US",
            state_code=None,
            city=None,
        )
        assert keys.company_title_location_hash is None

    def test_level_3_fires_when_specific(self, service: DedupeService) -> None:
        keys = service.compute(
            source="s",
            external_id="x",
            title="Quantum Cartographer",
            company_name="BigCo",
            company_external_id="1",
            apply_url=None,
            country_code="US",
            state_code="MI",
            city="Detroit",
        )
        assert keys.company_title_location_hash is not None

    def test_content_hash_changes_when_content_does(self, service: DedupeService) -> None:
        """Drives update detection: an unchanged hash means only last_seen_at moves."""
        base = {
            "source": "s",
            "external_id": "x",
            "title": "Engineer",
            "company_name": "ABC",
            "company_external_id": "1",
            "apply_url": "https://ex.com/1",
            "country_code": "US",
            "state_code": "MI",
            "city": "Detroit",
        }
        assert (
            service.compute(**base, salary_max=100).content_hash
            != service.compute(**base, salary_max=200).content_hash
        )
