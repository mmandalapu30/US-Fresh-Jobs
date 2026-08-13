"""US reference data tests.

These encode the spec's location requirements and the verified source quirks: mixed
state encodings ("Massachusetts" vs "OH"), junk country values ("", "REMOTE"), and the
Canadian/Mexican homograph problem.
"""

from __future__ import annotations

import pytest

from jobplatform_schemas.us_states import (
    AMBIGUOUS_CITY_NAMES,
    US_STATE_CODES,
    US_STATES,
    US_TERRITORIES,
    is_valid_state_code,
    resolve_country_code,
    resolve_state_code,
)


class TestStateCoverage:
    def test_fifty_states_plus_dc(self) -> None:
        """The spec requires all 50 states and Washington, D.C."""
        assert len(US_STATES) == 51
        assert US_STATES["DC"] == "District of Columbia"

    def test_no_duplicate_state_names(self) -> None:
        names = list(US_STATES.values())
        assert len(names) == len(set(names))

    def test_territories_are_separate_from_states(self) -> None:
        assert not set(US_TERRITORIES) & set(US_STATES)
        assert "PR" in US_STATE_CODES  # still a valid code overall


class TestResolveStateCode:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # The source emits full names AND codes in the same file (verified §4c).
            ("Massachusetts", "MA"),
            ("OH", "OH"),
            ("Michigan", "MI"),
            ("michigan", "MI"),
            ("  Michigan  ", "MI"),
            ("mi", "MI"),
            ("District of Columbia", "DC"),
            ("Washington DC", "DC"),
            ("washington d.c.", "DC"),
            ("New York", "NY"),
            ("Puerto Rico", "PR"),
        ],
    )
    def test_resolves(self, value: str, expected: str) -> None:
        assert resolve_state_code(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "Ontario",  # Canadian province
            "British Columbia",
            "Jalisco",  # Mexican state
            "ZZ",
            "Not A State",
        ],
    )
    def test_rejects_non_us(self, value: str | None) -> None:
        """Returning None forces the caller to handle 'unknown' rather than guess."""
        assert resolve_state_code(value) is None

    def test_ontario_is_not_oregon(self) -> None:
        """'ON' must not be coerced to a US state; Ontario is not Oregon."""
        assert resolve_state_code("Ontario") is None
        assert resolve_state_code("ON") is None

    def test_is_valid_state_code(self) -> None:
        assert is_valid_state_code("MI")
        assert is_valid_state_code("mi")
        assert not is_valid_state_code("ON")
        assert not is_valid_state_code(None)


class TestResolveCountryCode:
    @pytest.mark.parametrize(
        "value",
        [
            "United States",
            "united states",
            "USA",
            "US",
            "U.S.A.",
            "United States of America",
            "US Remote",
            "Remote - United States",
            "Remote (US)",
            "us remote",
            "Nationwide",
        ],
    )
    def test_us_aliases(self, value: str) -> None:
        assert resolve_country_code(value) == "US"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Canada", "CA"),
            ("Mexico", "MX"),
            ("United Kingdom", "GB"),
            ("India", "IN"),
            ("Germany", "DE"),
        ],
    )
    def test_non_us_countries(self, value: str, expected: str) -> None:
        assert resolve_country_code(value) == expected

    @pytest.mark.parametrize("value", [None, "", "   ", "REMOTE", "tbc", "-"])
    def test_junk_values_resolve_to_none(self, value: str | None) -> None:
        """Verified: the source ships '' (3,331 rows) and 'REMOTE' (270 rows) as country."""
        assert resolve_country_code(value) is None

    def test_state_code_in_country_field_is_not_a_country(self) -> None:
        """'CA' is ambiguous: California or Canada. A bare two-letter US state code in
        the country field must not silently become a country."""
        assert resolve_country_code("MI") is None
        assert resolve_country_code("TX") is None

    def test_canada_full_name_still_resolves(self) -> None:
        """The explicit alias wins, so real Canadian rows are still classified."""
        assert resolve_country_code("Canada") == "CA"


class TestAmbiguousCities:
    """The spec explicitly requires not misclassifying Canadian/Mexican cities."""

    @pytest.mark.parametrize(
        "city", ["london", "hamilton", "windsor", "vancouver", "richmond", "durango", "leon"]
    )
    def test_known_homographs_are_flagged(self, city: str) -> None:
        assert city in AMBIGUOUS_CITY_NAMES

    def test_flagged_names_are_lowercase(self) -> None:
        """Lookups normalize to lowercase; a capitalized entry would never match."""
        assert all(name == name.lower() for name in AMBIGUOUS_CITY_NAMES)

    def test_unambiguous_us_cities_not_flagged(self) -> None:
        for city in ("detroit", "chicago", "philadelphia", "seattle"):
            assert city not in AMBIGUOUS_CITY_NAMES
