"""LocationNormalizer — decide whether a job is in the United States, and where.

Source-agnostic on purpose: every connector hands over *raw* location strings and this
service applies one consistent policy, so a job from one source and a job from another
are judged by identical rules.

The hard part is not parsing "Detroit, MI". It is refusing to be fooled:

* The source's ``state`` field contains **non-US values** — ``Quebec`` and ``Maharashtra``
  are among the most common (verified on live data). A populated state never implies a
  US job.
* City names collide across borders: London ON vs London KY, Vancouver BC vs Vancouver WA.
  A city alone can therefore never promote a row to ``country_code = "US"``.
* ``country`` carries junk: empty strings (3,331 rows in one file) and the literal
  ``REMOTE`` (270 rows).

The policy is deliberately conservative: when the evidence is contradictory or thin, the
result is *not US*, and the row is rejected with a reason rather than guessed into the feed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from jobplatform_schemas import RemoteType
from jobplatform_schemas.us_states import (
    AMBIGUOUS_CITY_NAMES,
    NON_US_CITIES,
    NON_US_COUNTRY_ALIASES,
    US_COUNTRY_ALIASES,
    US_STATE_CODES,
    resolve_country_code,
    resolve_state_code,
)

__all__ = ["LocationNormalizer", "ResolvedLocation"]

#: Subdivisions that are definitively NOT US states. Seeing one in the state field is
#: positive evidence the job is foreign, not merely absent evidence that it is domestic.
#: Verified in live data: Quebec and Maharashtra rank in the top state values.
NON_US_SUBDIVISIONS: Final[frozenset[str]] = frozenset(
    {
        # Canada
        "ontario",
        "quebec",
        "québec",
        "british columbia",
        "alberta",
        "manitoba",
        "saskatchewan",
        "nova scotia",
        "new brunswick",
        "newfoundland and labrador",
        "prince edward island",
        "yukon",
        "nunavut",
        "northwest territories",
        "on",
        "qc",
        "bc",
        "ab",
        "mb",
        "sk",
        "ns",
        "nb",
        "nl",
        "pe",
        "yt",
        "nu",
        "nt",
        # India
        "maharashtra",
        "karnataka",
        "tamil nadu",
        "telangana",
        "gujarat",
        "delhi",
        "uttar pradesh",
        "west bengal",
        "haryana",
        "kerala",
        "rajasthan",
        "punjab",
        "andhra pradesh",
        "madhya pradesh",
        "bihar",
        "odisha",
        # Mexico
        "jalisco",
        "nuevo leon",
        "nuevo león",
        "ciudad de mexico",
        "ciudad de méxico",
        "estado de mexico",
        "guanajuato",
        "puebla",
        "queretaro",
        "querétaro",
        "baja california",
        "chihuahua",
        "yucatan",
        "yucatán",
        # UK / IE
        "england",
        "scotland",
        "wales",
        "northern ireland",
        "greater london",
        "leinster",
        "munster",
        "connacht",
        "ulster",
        # Australia / NZ
        "new south wales",
        "victoria",
        "queensland",
        "western australia",
        "south australia",
        "tasmania",
        "australian capital territory",
        "nsw",
        "qld",
        "vic",
        "wa-au",
        "auckland",
        "wellington",
        "canterbury",
        # Other frequent ones
        "bavaria",
        "bayern",
        "hesse",
        "hessen",
        "catalonia",
        "cataluña",
        "madrid",
        "île-de-france",
        "ile-de-france",
        "lombardy",
        "lombardia",
        "sao paulo",
        "são paulo",
        "gauteng",
        "western cape",
    }
)

#: City aliases the spec explicitly calls out, plus the obvious metro shorthands.
CITY_ALIASES: Final[dict[str, tuple[str, str]]] = {
    "nyc": ("New York", "NY"),
    "new york city": ("New York", "NY"),
    "manhattan": ("New York", "NY"),
    "brooklyn": ("New York", "NY"),
    "queens": ("New York", "NY"),
    "the bronx": ("New York", "NY"),
    "bronx": ("New York", "NY"),
    "staten island": ("New York", "NY"),
    "sf": ("San Francisco", "CA"),
    "san fran": ("San Francisco", "CA"),
    "bay area": ("San Francisco", "CA"),
    "sfo": ("San Francisco", "CA"),
    "la": ("Los Angeles", "CA"),
    "los angeles": ("Los Angeles", "CA"),
    "dc": ("Washington", "DC"),
    "washington dc": ("Washington", "DC"),
    "washington d.c.": ("Washington", "DC"),
    "philly": ("Philadelphia", "PA"),
    "chi-town": ("Chicago", "IL"),
    "atl": ("Atlanta", "GA"),
    "nola": ("New Orleans", "LA"),
    "vegas": ("Las Vegas", "NV"),
    "las vegas": ("Las Vegas", "NV"),
}

#: Words signalling remote work anywhere in a free-text location.
_REMOTE_TOKENS: Final = re.compile(
    r"\b(remote|work\s*from\s*home|wfh|telecommute|anywhere|distributed|virtual)\b",
    re.IGNORECASE,
)
_HYBRID_TOKENS: Final = re.compile(r"\b(hybrid|flex(?:ible)?\s*(?:work|location))\b", re.IGNORECASE)
_ONSITE_TOKENS: Final = re.compile(
    r"\b(on[\s-]?site|in[\s-]?office|in[\s-]?person)\b", re.IGNORECASE
)

#: US ZIP, optionally ZIP+4.
_ZIP_RE: Final = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

#: "City, ST", "City, State Name", or "City, ST 45324" — the dominant free-text shapes.
#: The optional trailing ZIP matters: "Fairborn, OH 45324" is common, and without this the
#: state group would capture "OH 45324" and fail to resolve.
_CITY_STATE_RE: Final = re.compile(
    r"^\s*(?P<city>[^,]+?)\s*,\s*(?P<state>[A-Za-z .]{2,30}?)\s*(?:\d{5}(?:-\d{4})?)?\s*$"
)


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """The outcome of normalization.

    ``is_us`` is the gate the ingestion pipeline filters on. ``reason`` explains a
    negative result so a rejection is never mysterious.
    """

    country_code: str | None
    state_code: str | None
    city: str | None
    city_normalized: str | None
    postal_code: str | None
    remote_type: RemoteType
    raw_text: str | None
    #: Why the row was classified as it was. Populated on rejection and on ambiguity.
    reason: str | None = None

    @property
    def is_us(self) -> bool:
        return self.country_code == "US"

    @property
    def is_remote(self) -> bool:
        return self.remote_type is RemoteType.REMOTE


class LocationNormalizer:
    """Turns raw source location fields into a normalized, trustworthy location."""

    def normalize(
        self,
        *,
        raw_country: str | None = None,
        raw_state: str | None = None,
        raw_city: str | None = None,
        raw_postal_code: str | None = None,
        raw_location_text: str | None = None,
        raw_workplace_type: str | None = None,
        source_is_remote: bool | None = None,
        company_country: str | None = None,
    ) -> ResolvedLocation:
        text = (raw_location_text or "").strip()
        remote_type = self._resolve_remote(
            raw_workplace_type=raw_workplace_type,
            source_is_remote=source_is_remote,
            text=text,
        )

        # --- 1. Is the state field positive evidence of a FOREIGN job? ---------
        # Checked first because it is the strongest disqualifying signal: a country of
        # "United States" alongside a state of "Quebec" is a contradiction, and trusting
        # the country field would put a Canadian job in the US feed.
        state_raw = (raw_state or "").strip()
        if state_raw and state_raw.lower() in NON_US_SUBDIVISIONS:
            return ResolvedLocation(
                country_code=None,
                state_code=None,
                city=self._clean_city(raw_city),
                city_normalized=self._normalize_city_key(raw_city),
                postal_code=None,
                remote_type=remote_type,
                raw_text=text or None,
                reason=f"state {state_raw!r} is not a US subdivision",
            )

        # --- 2. Country -------------------------------------------------------
        country_code = resolve_country_code(raw_country)

        # Fall back to the free-text blob when the country field is junk ("", "REMOTE").
        if country_code is None and text:
            country_code = self._country_from_text(text)

        # --- 3. State ---------------------------------------------------------
        state_code = resolve_state_code(state_raw)
        city = self._clean_city(raw_city)

        # Parse "Detroit, MI" out of the free text when the structured fields are empty.
        if (state_code is None or city is None) and text:
            parsed_city, parsed_state = self._parse_city_state(text)
            city = city or parsed_city
            state_code = state_code or parsed_state

        # The source sometimes puts the state in the city field (observed: city="TX").
        # Only a bare two-letter code is promoted: no US city is named "TX", whereas full
        # state names are genuinely ambiguous as city names (New York, Washington,
        # Wyoming MI, Delaware OH), so promoting those would corrupt real cities.
        if city and state_code is None and len(city.strip()) == 2:
            promoted = resolve_state_code(city)
            if promoted:
                state_code = promoted
                city = None

        # Resolve metro shorthands ("NYC", "Bay Area") which also carry a state.
        if city:
            alias = CITY_ALIASES.get(city.strip().lower())
            if alias is not None:
                city, alias_state = alias[0], alias[1]
                state_code = state_code or alias_state

        # A valid US state is positive evidence of a US job, even when country was junk.
        if state_code and state_code in US_STATE_CODES and country_code is None:
            country_code = "US"

        # --- 3b. Is the CITY positive evidence of a foreign job? --------------
        # The source's country field is not trustworthy: 317 Toronto, 155 Montreal and
        # 45 Mississauga jobs all arrived labelled "United States". A definitively foreign
        # city therefore overrides the country field rather than deferring to it.
        city_key = (city or "").strip().lower()
        if city_key and city_key in NON_US_CITIES:
            return ResolvedLocation(
                country_code=None,
                state_code=None,
                city=city,
                city_normalized=self._normalize_city_key(city),
                postal_code=None,
                remote_type=remote_type,
                raw_text=text or None,
                reason=f"city {city!r} is not a US city despite country={raw_country!r}",
            )

        # The employer's own registered country is stronger evidence than the per-job
        # country field, which the source frequently mislabels. A curated city blocklist
        # can never be complete -- Burlington, Richmond Hill, Collingwood and Cornwall are
        # all Ontario towns that no reasonable list would contain -- so when a job claims
        # the US but carries no state to prove it, the employer's country decides.
        employer_country = resolve_country_code(company_country)
        if (
            country_code == "US"
            and state_code is None
            and employer_country is not None
            and employer_country != "US"
        ):
            return ResolvedLocation(
                country_code=None,
                state_code=None,
                city=city,
                city_normalized=self._normalize_city_key(city),
                postal_code=None,
                remote_type=remote_type,
                raw_text=text or None,
                reason=(
                    f"employer is registered in {employer_country} and no US state "
                    f"confirms this posting"
                ),
            )

        # An ambiguous city with no state cannot be confirmed as US. Ottawa ON and Ottawa
        # IL are indistinguishable here, and putting a Canadian job in a US-only product
        # is worse than withholding an unconfirmable one -- the rejection is recorded, so
        # nothing is lost silently.
        if country_code == "US" and state_code is None and city_key in AMBIGUOUS_CITY_NAMES:
            return ResolvedLocation(
                country_code=None,
                state_code=None,
                city=city,
                city_normalized=self._normalize_city_key(city),
                postal_code=None,
                remote_type=remote_type,
                raw_text=text or None,
                reason=f"city {city!r} is ambiguous across countries and no state confirms US",
            )

        # --- 4. Contradictions ------------------------------------------------
        if country_code and country_code != "US" and state_code:
            # A recognised foreign country wins over a state code that merely looks US-ish
            # ("CA" is California *and* Canada).
            state_code = None

        # --- 5. Remote-US phrasing -------------------------------------------
        # "US Remote" / "Remote - United States" carry the country in the phrase itself.
        if country_code is None and text and self._is_us_remote_phrase(text):
            country_code = "US"

        # --- 6. City alone must never promote to US --------------------------
        # This is the Canadian/Mexican homograph defence the spec requires.
        if country_code is None and city:
            return ResolvedLocation(
                country_code=None,
                state_code=None,
                city=city,
                city_normalized=self._normalize_city_key(city),
                postal_code=None,
                remote_type=remote_type,
                raw_text=text or None,
                reason=(
                    f"city {city!r} is ambiguous across countries; no country or state signal"
                    if city.strip().lower() in AMBIGUOUS_CITY_NAMES
                    else "no country or state signal; a city alone cannot imply a country"
                ),
            )

        postal_code = self._resolve_postal(raw_postal_code, text, country_code)

        return ResolvedLocation(
            country_code=country_code,
            state_code=state_code if country_code == "US" else None,
            city=city,
            city_normalized=self._normalize_city_key(city),
            postal_code=postal_code,
            remote_type=remote_type,
            raw_text=text or None,
            reason=None if country_code else "country could not be determined",
        )

    # ---- helpers -------------------------------------------------------------

    @staticmethod
    def _resolve_remote(
        *, raw_workplace_type: str | None, source_is_remote: bool | None, text: str
    ) -> RemoteType:
        """Prefer the structured field; fall back to the text; never guess ONSITE.

        The source's ``tbc`` is already mapped to ``None`` by the connector, so an absent
        workplace type genuinely means unknown — and calling that ONSITE would misfile
        roughly 41% of rows.
        """
        workplace = (raw_workplace_type or "").strip().lower()
        if workplace in {"remote", "fully remote"}:
            return RemoteType.REMOTE
        if workplace == "hybrid":
            return RemoteType.HYBRID
        if workplace in {"onsite", "on-site", "on site", "in office", "in-office"}:
            return RemoteType.ONSITE

        if source_is_remote is True:
            return RemoteType.REMOTE

        if text:
            if _HYBRID_TOKENS.search(text):
                return RemoteType.HYBRID
            if _REMOTE_TOKENS.search(text):
                return RemoteType.REMOTE
            if _ONSITE_TOKENS.search(text):
                return RemoteType.ONSITE

        return RemoteType.UNKNOWN

    @staticmethod
    def _country_from_text(text: str) -> str | None:
        """Find a country signal inside a free-text location.

        Longest alias first, so "united states of america" is not shadowed by "us".
        """
        lowered = text.lower()

        for alias in sorted(US_COUNTRY_ALIASES, key=len, reverse=True):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
                return "US"

        for alias, code in sorted(
            NON_US_COUNTRY_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
                return code

        return None

    @staticmethod
    def _is_us_remote_phrase(text: str) -> bool:
        lowered = text.lower()
        return bool(_REMOTE_TOKENS.search(lowered)) and bool(
            re.search(r"(?<![a-z])(us|usa|u\.s\.|united states|america)(?![a-z])", lowered)
        )

    @staticmethod
    def _parse_city_state(text: str) -> tuple[str | None, str | None]:
        """Extract ``City, ST`` / ``City, State Name`` from free text.

        Only the last comma-separated pair is considered, so "Remote - Detroit, MI" and
        "Building 4, Detroit, MI" both work.
        """
        cleaned = text.strip().strip("-").strip()
        if not cleaned:
            return None, None

        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        if len(parts) >= 2:
            candidate = f"{parts[-2]}, {parts[-1]}"
            match = _CITY_STATE_RE.match(candidate)
            if match:
                state = resolve_state_code(match.group("state"))
                if state:
                    return match.group("city").strip() or None, state

        # A bare state name on its own line: "            South Carolina"
        state = resolve_state_code(cleaned)
        if state:
            return None, state

        return None, None

    @staticmethod
    def _resolve_postal(
        raw_postal_code: str | None, text: str, country_code: str | None
    ) -> str | None:
        """Return a US ZIP when one is available and the job is US.

        Postal codes are only stored for the US in v1; keeping a foreign format in a
        column the UI treats as a ZIP would produce nonsense filters.
        """
        if country_code != "US":
            return None

        candidate = (raw_postal_code or "").strip()
        if candidate:
            match = _ZIP_RE.search(candidate)
            if match:
                return match.group(1)

        if text:
            match = _ZIP_RE.search(text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _clean_city(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = re.sub(r"\s+", " ", value).strip(" ,-\t\n")
        return cleaned or None

    @staticmethod
    def _normalize_city_key(value: str | None) -> str | None:
        """Lowercase, punctuation-stripped key used for the location dimension.

        "St. Louis", "St Louis" and "st.  louis" must all collapse to one row, otherwise
        the dimension table fragments and city filters miss results.
        """
        if not value:
            return None
        lowered = re.sub(r"[^\w\s]", "", value.lower())
        collapsed = re.sub(r"\s+", " ", lowered).strip()
        return collapsed or None
