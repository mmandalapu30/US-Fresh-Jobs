"""US state reference data and country-name resolution.

This module is pure data plus exact lookups. The fuzzy work (parsing "Detroit, Michigan"
or "US Remote") lives in ``LocationNormalizer`` in the ingestion worker (Milestone 5);
keeping the reference tables here means the API, the worker and the frontend agree on
what a valid ``state_code`` is.

Verified motivation (docs/00-source-verification.md §4c): the source emits state as both
``"Massachusetts"`` and ``"OH"`` in the same file, so both forms must resolve.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AMBIGUOUS_CITY_NAMES",
    "NON_US_COUNTRY_ALIASES",
    "STATE_CODE_TO_NAME",
    "STATE_NAME_TO_CODE",
    "US_COUNTRY_ALIASES",
    "US_STATES",
    "US_STATE_CODES",
    "US_TERRITORIES",
    "is_valid_state_code",
    "resolve_country_code",
    "resolve_state_code",
]

#: The 50 states plus the District of Columbia. Matches the spec requirement exactly.
US_STATES: Final[dict[str, str]] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

#: Inhabited territories. Kept separate from ``US_STATES`` because most product filters
#: mean "the 50 + DC", but these are still ``country_code == "US"``.
US_TERRITORIES: Final[dict[str, str]] = {
    "PR": "Puerto Rico",
    "VI": "U.S. Virgin Islands",
    "GU": "Guam",
    "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}

STATE_CODE_TO_NAME: Final[dict[str, str]] = {**US_STATES, **US_TERRITORIES}

#: Reverse lookup, lowercase-keyed, including common written variants.
STATE_NAME_TO_CODE: Final[dict[str, str]] = {
    **{name.lower(): code for code, name in STATE_CODE_TO_NAME.items()},
    "washington dc": "DC",
    "washington d.c.": "DC",
    "washington, d.c.": "DC",
    "d.c.": "DC",
    "dc": "DC",
    "district of columbia": "DC",
    "puerto rico": "PR",
    "us virgin islands": "VI",
    "u.s. virgin islands": "VI",
    "virgin islands": "VI",
    "northern mariana islands": "MP",
    "american samoa": "AS",
}

US_STATE_CODES: Final[frozenset[str]] = frozenset(STATE_CODE_TO_NAME)

#: Strings that unambiguously denote the United States. Lowercase keys.
US_COUNTRY_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "us",
        "usa",
        "u.s.",
        "u.s.a.",
        "united states",
        "united states of america",
        "the united states",
        "the united states of america",
        "america",
        "us remote",
        "remote - united states",
        "remote - us",
        "remote (us)",
        "remote, us",
        "remote us",
        "united states (remote)",
        "usa remote",
        "anywhere in the us",
        "anywhere in the united states",
        "nationwide",
        "us-remote",
    }
)

#: Countries we must NOT classify as US. Present because the source ships Canadian and
#: Mexican rows whose cities collide with US city names (see AMBIGUOUS_CITY_NAMES).
NON_US_COUNTRY_ALIASES: Final[dict[str, str]] = {
    "canada": "CA",
    "ca-canada": "CA",
    "mexico": "MX",
    "méxico": "MX",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "ireland": "IE",
    "india": "IN",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "spain": "ES",
    "españa": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "australia": "AU",
    "new zealand": "NZ",
    "brazil": "BR",
    "brasil": "BR",
    "china": "CN",
    "japan": "JP",
    "singapore": "SG",
    "philippines": "PH",
    "poland": "PL",
    "kenya": "KE",
    "south africa": "ZA",
    "argentina": "AR",
    "colombia": "CO",
    "chile": "CL",
    "portugal": "PT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "israel": "IL",
    "united arab emirates": "AE",
    "uae": "AE",
    "pakistan": "PK",
    "bangladesh": "BD",
    "indonesia": "ID",
    "vietnam": "VN",
    "malaysia": "MY",
    "thailand": "TH",
    "south korea": "KR",
    "korea": "KR",
    "turkey": "TR",
    "egypt": "EG",
    "nigeria": "NG",
    "ghana": "GH",
    "romania": "RO",
    "ukraine": "UA",
    "czech republic": "CZ",
    "czechia": "CZ",
    "hungary": "HU",
    "greece": "GR",
}

#: City names that exist in the US *and* in Canada or Mexico. A city name alone can never
#: promote a row to ``country_code = "US"``; a state or country signal is required.
#: This is the concrete defence against the spec's "do not misclassify Canadian or Mexican
#: cities as U.S. jobs" requirement.
AMBIGUOUS_CITY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "ottawa",  # Ottawa, ON  vs Ottawa, IL / KS / OH
        "richmond hill",  # Richmond Hill, ON vs Richmond Hill, NY (Queens) / GA
        "london",  # London, ON  vs London, KY
        "hamilton",  # Hamilton, ON vs Hamilton, OH
        "windsor",  # Windsor, ON  vs Windsor, CT
        "kingston",  # Kingston, ON vs Kingston, NY
        "waterloo",  # Waterloo, ON vs Waterloo, IA
        "cambridge",  # Cambridge, ON vs Cambridge, MA
        "victoria",  # Victoria, BC vs Victoria, TX
        "vancouver",  # Vancouver, BC vs Vancouver, WA
        "richmond",  # Richmond, BC vs Richmond, VA
        "surrey",  # Surrey, BC   vs Surrey (UK)
        "guelph",
        "barrie",
        "aurora",  # Aurora, ON   vs Aurora, CO
        "newmarket",
        "oakville",
        "milton",  # Milton, ON   vs Milton, MA
        "st. catharines",
        "chatham",  # Chatham, ON  vs Chatham, NJ
        "sarnia",
        "welland",
        "brantford",
        "peterborough",
        "kitchener",
        "durango",  # Durango, MX  vs Durango, CO
        "monterrey",
        "guadalajara",
        "leon",  # León, MX     vs Leon, IA
        "toledo",  # Toledo, ES   vs Toledo, OH
        "cordoba",
        "santiago",
        "birmingham",  # Birmingham, UK vs Birmingham, AL
        "manchester",  # Manchester, UK vs Manchester, NH
        "boston",  # Boston, UK   vs Boston, MA
        "bristol",  # Bristol, UK  vs Bristol, CT
        "newport",
        "plymouth",
        "dublin",  # Dublin, IE   vs Dublin, OH
        "athens",  # Athens, GR   vs Athens, GA
        "moscow",  # Moscow, RU   vs Moscow, ID
        "paris",  # Paris, FR    vs Paris, TX
        "melbourne",  # Melbourne, AU vs Melbourne, FL
        "perth",
        "hyderabad",
        "york",  # York, UK     vs York, PA
        "lancaster",
        "lincoln",  # Lincoln, UK  vs Lincoln, NE
        "reading",  # Reading, UK  vs Reading, PA
        "oxford",  # Oxford, UK   vs Oxford, MS
    }
)


#: Major cities with **no significant US counterpart**. Seeing one is positive evidence the
#: job is foreign, which outranks a ``country`` field claiming otherwise.
#:
#: This list exists because the source really does mislabel: 317 Toronto jobs, 155 Montreal
#: and 45 Mississauga all arrived with ``country = "United States"``. Trusting that field
#: put Canadian grocery-store jobs in a US-only product.
#:
#: Deliberately excludes ambiguous names (Ottawa IL/KS/OH, Vancouver WA, London KY,
#: Cambridge MA) — those are handled by AMBIGUOUS_CITY_NAMES, which requires a state to
#: confirm rather than assuming either way.
NON_US_CITIES: Final[frozenset[str]] = frozenset(
    {
        # Canada
        "toronto",
        "montreal",
        "montréal",
        "mississauga",
        "brampton",
        "scarborough",
        "etobicoke",
        "north york",
        "calgary",
        "edmonton",
        "winnipeg",
        "halifax",
        "saskatoon",
        "regina",
        "quebec city",
        "québec city",
        "laval",
        "gatineau",
        "burnaby",
        "coquitlam",
        "markham",
        "vaughan",
        "oshawa",
        "ajax",
        "pickering",
        "whitby",
        "sherbrooke",
        "trois-rivieres",
        "trois-rivières",
        "moncton",
        "st. john's",
        "saint john",
        "thunder bay",
        "sudbury",
        "kelowna",
        "abbotsford",
        # Mexico
        "mexico city",
        "ciudad de mexico",
        "ciudad de méxico",
        "guadalajara",
        "monterrey",
        "tijuana",
        "puebla",
        "merida",
        "mérida",
        "cancun",
        "cancún",
        "queretaro",
        "querétaro",
        "leon guanajuato",
        # UK / IE
        "manchester uk",
        "liverpool",
        "leeds",
        "sheffield",
        "edinburgh",
        "glasgow",
        "cardiff",
        "belfast",
        "nottingham",
        "leicester",
        "coventry",
        "bradford",
        "dublin ireland",
        "cork",
        "galway",
        # Elsewhere, frequent in this dataset
        "bengaluru",
        "bangalore",
        "mumbai",
        "pune",
        "chennai",
        "hyderabad india",
        "kolkata",
        "gurgaon",
        "gurugram",
        "noida",
        "ahmedabad",
        "sydney",
        "brisbane",
        "adelaide",
        "canberra",
        "auckland",
        "christchurch",
        "singapore",
        "hong kong",
        "shanghai",
        "beijing",
        "shenzhen",
        "tokyo",
        "osaka",
        "seoul",
        "manila",
        "jakarta",
        "bangkok",
        "kuala lumpur",
        "ho chi minh city",
        "sao paulo",
        "são paulo",
        "rio de janeiro",
        "buenos aires",
        "bogota",
        "bogotá",
        "santiago de chile",
        "lima",
        "mexico df",
        "amsterdam",
        "rotterdam",
        "brussels",
        "antwerp",
        "copenhagen",
        "stockholm",
        "oslo",
        "helsinki",
        "warsaw",
        "krakow",
        "kraków",
        "prague",
        "budapest",
        "bucharest",
        "sofia",
        "zagreb",
        "vienna",
        "zurich",
        "zürich",
        "geneva",
        "munich",
        "münchen",
        "berlin",
        "hamburg",
        "frankfurt",
        "cologne",
        "köln",
        "stuttgart",
        "dusseldorf",
        "düsseldorf",
        "milan",
        "milano",
        "rome",
        "roma",
        "turin",
        "torino",
        "naples",
        "napoli",
        "barcelona",
        "valencia",
        "seville",
        "lisbon",
        "porto",
        "athens greece",
        "istanbul",
        "ankara",
        "tel aviv",
        "dubai",
        "abu dhabi",
        "doha",
        "riyadh",
        "cairo",
        "lagos",
        "nairobi",
        "johannesburg",
        "cape town",
        "casablanca",
        "accra",
    }
)


def resolve_state_code(value: str | None) -> str | None:
    """Resolve a state code or full state name to its two-letter code.

    Handles the verified inconsistency where the source emits both ``"Massachusetts"``
    and ``"OH"``. Returns ``None`` when the value is not a recognised US state, which the
    caller must treat as "unknown", never as a silent default.

    >>> resolve_state_code("Michigan")
    'MI'
    >>> resolve_state_code("mi")
    'MI'
    >>> resolve_state_code("Ontario") is None
    True
    """
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None

    upper = cleaned.upper()
    if len(upper) == 2 and upper in US_STATE_CODES:
        return upper

    return STATE_NAME_TO_CODE.get(cleaned.lower())


def is_valid_state_code(value: str | None) -> bool:
    return bool(value) and value.strip().upper() in US_STATE_CODES  # type: ignore[union-attr]


def resolve_country_code(value: str | None) -> str | None:
    """Resolve a free-text country string to an ISO-3166-1 alpha-2 code.

    Returns ``None`` for unrecognised or junk input. The source emits empty strings and
    literal ``"REMOTE"`` in this field (verified), and both must resolve to ``None``
    rather than being guessed at.

    >>> resolve_country_code("United States")
    'US'
    >>> resolve_country_code("REMOTE") is None
    True
    """
    if not value:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None

    if cleaned in US_COUNTRY_ALIASES:
        return "US"
    if cleaned in NON_US_COUNTRY_ALIASES:
        return NON_US_COUNTRY_ALIASES[cleaned]

    # A bare two-letter code, but only if it is a plausible country code and not a US
    # state abbreviation being passed in the wrong field.
    upper = cleaned.upper()
    if len(upper) == 2 and upper.isalpha() and upper not in US_STATE_CODES:
        return upper

    return None
