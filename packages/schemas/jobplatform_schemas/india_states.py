"""India subdivision reference data and city-to-state resolution.

Mirrors ``us_states.py``: pure data plus exact lookups, kept in the shared package so the
API, the worker and the frontend agree on what a valid Indian ``state_code`` is.

Codes are ISO 3166-2:IN without the ``IN-`` prefix, so Maharashtra is ``MH``.

**India's country code is ``IN``, which is also Indiana's state code.** The two live in
different columns (``jobs.country_code`` and ``jobs.state_code``) and must never be
resolved from the same string, which is why ``resolve_state_code`` (US) and
``resolve_india_state_code`` are separate functions rather than one lookup over a merged
table. A merged table would make "IN" mean either, decided by whichever entry was written
last.

The city map exists because the source routinely gives an Indian city and nothing else --
"Bengaluru" with country "India" and an empty state field. Only cities whose subdivision is
unambiguous are listed; a city that exists in two states does not belong here.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "INDIA_CITY_TO_STATE",
    "INDIA_STATES",
    "INDIA_STATE_CODES",
    "is_valid_india_state_code",
    "resolve_india_state_code",
    "resolve_india_state_from_city",
]

#: The 28 states and 8 union territories.
INDIA_STATES: Final[dict[str, str]] = {
    "AN": "Andaman and Nicobar Islands",
    "AP": "Andhra Pradesh",
    "AR": "Arunachal Pradesh",
    "AS": "Assam",
    "BR": "Bihar",
    "CH": "Chandigarh",
    "CT": "Chhattisgarh",
    "DH": "Dadra and Nagar Haveli and Daman and Diu",
    "DL": "Delhi",
    "GA": "Goa",
    "GJ": "Gujarat",
    "HP": "Himachal Pradesh",
    "HR": "Haryana",
    "JH": "Jharkhand",
    "JK": "Jammu and Kashmir",
    "KA": "Karnataka",
    "KL": "Kerala",
    "LA": "Ladakh",
    "LD": "Lakshadweep",
    "MH": "Maharashtra",
    "ML": "Meghalaya",
    "MN": "Manipur",
    "MP": "Madhya Pradesh",
    "MZ": "Mizoram",
    "NL": "Nagaland",
    "OD": "Odisha",
    "PB": "Punjab",
    "PY": "Puducherry",
    "RJ": "Rajasthan",
    "SK": "Sikkim",
    "TG": "Telangana",
    "TN": "Tamil Nadu",
    "TR": "Tripura",
    "UK": "Uttarakhand",
    "UP": "Uttar Pradesh",
    "WB": "West Bengal",
}

INDIA_STATE_CODES: Final[frozenset[str]] = frozenset(INDIA_STATES)

#: Older or colloquial names the source still uses. "NCR" and "Delhi NCR" describe a metro
#: region spanning three states; they resolve to Delhi because that is the only part of it
#: a reader would call anything else.
_ALIASES: Final[dict[str, str]] = {
    "orissa": "OD",
    "pondicherry": "PY",
    "puducherry": "PY",
    "uttaranchal": "UK",
    "ncr": "DL",
    "delhi ncr": "DL",
    "new delhi": "DL",
    "national capital region": "DL",
    "jammu & kashmir": "JK",
    "andaman & nicobar islands": "AN",
    "tamilnadu": "TN",
    "telengana": "TG",
    "karnatak": "KA",
}

_NAME_TO_CODE: Final[dict[str, str]] = {
    **{name.lower(): code for code, name in INDIA_STATES.items()},
    **_ALIASES,
}

#: City -> subdivision, for rows that carry a city and no state. Restricted to cities whose
#: state is unambiguous; the frequent ones in this dataset are listed first because they are
#: the reason the map exists (see ``NON_US_CITIES`` in ``us_states``, where these same names
#: appear as evidence a job is *not* American).
INDIA_CITY_TO_STATE: Final[dict[str, str]] = {
    "bengaluru": "KA",
    "bangalore": "KA",
    "mysuru": "KA",
    "mysore": "KA",
    "mumbai": "MH",
    "navi mumbai": "MH",
    "thane": "MH",
    "pune": "MH",
    "nagpur": "MH",
    "nashik": "MH",
    "chennai": "TN",
    "coimbatore": "TN",
    "madurai": "TN",
    "hyderabad": "TG",
    "secunderabad": "TG",
    "kolkata": "WB",
    "calcutta": "WB",
    "gurgaon": "HR",
    "gurugram": "HR",
    "faridabad": "HR",
    "noida": "UP",
    "greater noida": "UP",
    "ghaziabad": "UP",
    "lucknow": "UP",
    "kanpur": "UP",
    "ahmedabad": "GJ",
    "surat": "GJ",
    "vadodara": "GJ",
    "gandhinagar": "GJ",
    "delhi": "DL",
    "new delhi": "DL",
    "kochi": "KL",
    "cochin": "KL",
    "thiruvananthapuram": "KL",
    "trivandrum": "KL",
    "kozhikode": "KL",
    "jaipur": "RJ",
    "indore": "MP",
    "bhopal": "MP",
    "bhubaneswar": "OD",
    "chandigarh": "CH",
    "patna": "BR",
    "raipur": "CT",
    "ranchi": "JH",
    "guwahati": "AS",
    "dehradun": "UK",
    "goa": "GA",
    "panaji": "GA",
    "visakhapatnam": "AP",
    "vijayawada": "AP",
    "amritsar": "PB",
    "ludhiana": "PB",
    "mohali": "PB",
}


def is_valid_india_state_code(value: str | None) -> bool:
    return bool(value) and value.strip().upper() in INDIA_STATE_CODES


def resolve_india_state_code(value: str | None) -> str | None:
    """Resolve a subdivision name or code to its ISO code.

    Accepts "Maharashtra", "maharashtra" and "MH". Returns None for anything else --
    including valid US state codes, so an Indian row carrying "CA" resolves to nothing
    rather than quietly becoming California.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if cleaned.upper() in INDIA_STATE_CODES:
        return cleaned.upper()
    return _NAME_TO_CODE.get(cleaned.lower())


def resolve_india_state_from_city(city: str | None) -> str | None:
    """Resolve an unambiguous Indian city to its subdivision, or None."""
    cleaned = (city or "").strip().lower()
    if not cleaned:
        return None
    return INDIA_CITY_TO_STATE.get(cleaned)
