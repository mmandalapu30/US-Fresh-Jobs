"""Deduplication — four levels, from certain to heuristic.

The spec's warning matters more than the matching itself: *do not incorrectly merge two
legitimately different jobs*. A false merge silently deletes a real opening from the feed
and is invisible in the metrics, whereas a missed duplicate is merely untidy. Every rule
here is therefore biased toward keeping things separate.

| Level | Key | Confidence |
|-------|-----|-----------|
| L1 | ``(source, external_job_id)`` | Certain — a database UNIQUE |
| L2 | canonical apply URL | Very high — same URL is the same application |
| L3 | company + title + location | Moderate — gated, see below |
| L4 | content fingerprint | Moderate — identical substance |

**Why L3 is gated.** "Software Engineer" at a large employer in "New York, NY" is routinely
several genuinely different roles. So L3 only fires when the location is specific (a city,
not just a country) *and* the title is not generic. Without those guards it would merge
sibling postings and lose openings.

This module computes keys only. Applying them — deciding which job survives a merge and
recording it in ``job_events`` — belongs to the loader, which owns the transaction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = ["DedupeKeys", "DedupeService"]

#: Tracking parameters carry no application identity, so two URLs differing only by these
#: are the same job. Stripping them is what makes L2 work at all.
_TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gh_src",
        "gh_jid",
        "lever-source",
        "lever-origin",
        "source",
        "src",
        "ref",
        "referrer",
        "referer",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "trackingid",
        "tracking_id",
        "campaign",
        "medium",
        "recruiter",
        "rx_source",
        "rx_campaign",
        "rx_medium",
        "rx_group",
        "rx_job",
        "sourcetype",
        "iis",
        "iisn",
    }
)

#: Titles too generic for L3 to be safe on their own.
_GENERIC_TITLES: Final[frozenset[str]] = frozenset(
    {
        "software engineer",
        "engineer",
        "developer",
        "manager",
        "analyst",
        "consultant",
        "associate",
        "specialist",
        "coordinator",
        "administrator",
        "technician",
        "sales",
        "sales representative",
        "account manager",
        "project manager",
        "intern",
        "internship",
        "customer service",
        "customer service representative",
        "server",
        "cashier",
        "cook",
        "driver",
        "nurse",
        "registered nurse",
        "team member",
        "crew member",
        "warehouse associate",
        "security officer",
        "general manager",
        "assistant manager",
        "shift supervisor",
        "receptionist",
        "data analyst",
        "data scientist",
        "product manager",
        "designer",
    }
)

#: Noise that varies between postings of the same role.
_TITLE_NOISE: Final = re.compile(
    r"\b(senior|sr\.?|junior|jr\.?|lead|principal|staff|i{1,3}|iv|v|[0-9]+)\b"
    r"|\((?:[^)]*)\)"
    r"|\[[^\]]*\]"
    # Trailing clause after a dash or pipe. The dashes are written as codepoints so the
    # character class is unambiguous in source: en dash and em dash are visually identical
    # to a hyphen in most editors but are different characters.
    r"|[-\u2013\u2014|].*$",
    re.IGNORECASE,
)

_LEGAL_SUFFIXES: Final = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"gmbh|plc|sa|nv|bv|ag|pty|llp|lp|holdings|group|international|worldwide)\b\.?",
    re.IGNORECASE,
)

_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class DedupeKeys:
    """The keys a job is matched on. ``None`` means "this level cannot be used"."""

    #: L1 — always available.
    source_key: tuple[str, str]
    #: L2 — sha256 of the canonical apply URL.
    apply_url_hash: bytes | None
    #: L3 — sha256 of company+title+location, only when the guards pass.
    company_title_location_hash: bytes | None
    #: L4 — sha256 of the substantive content.
    content_fingerprint: bytes
    #: Detects real change between observations of the *same* job. Not a dedupe key.
    content_hash: bytes
    canonical_apply_url: str | None


class DedupeService:
    """Computes deterministic dedupe keys for a normalized job."""

    def compute(
        self,
        *,
        source: str,
        external_id: str,
        title: str,
        company_name: str | None,
        company_external_id: str | None,
        apply_url: str | None,
        country_code: str | None,
        state_code: str | None,
        city: str | None,
        description_text: str | None = None,
        salary_min: float | None = None,
        salary_max: float | None = None,
        remote_type: str | None = None,
        employment_type: str | None = None,
    ) -> DedupeKeys:
        canonical_url = self.canonicalize_url(apply_url)
        company_key = self.normalize_company(company_name) or (company_external_id or "")
        title_key = self.normalize_title(title)

        return DedupeKeys(
            source_key=(source, external_id),
            apply_url_hash=_sha256(canonical_url) if canonical_url else None,
            company_title_location_hash=self._l3_hash(
                company_key=company_key,
                title_key=title_key,
                title_raw=title,
                country_code=country_code,
                state_code=state_code,
                city=city,
            ),
            content_fingerprint=_sha256(
                "|".join(
                    [
                        company_key,
                        title_key,
                        (country_code or ""),
                        (state_code or ""),
                        _normalize_key(city or ""),
                        _digest_text(description_text),
                    ]
                )
            ),
            content_hash=_sha256(
                "|".join(
                    [
                        title.strip(),
                        company_key,
                        (country_code or ""),
                        (state_code or ""),
                        (city or "").strip(),
                        f"{salary_min or ''}",
                        f"{salary_max or ''}",
                        (remote_type or ""),
                        (employment_type or ""),
                        canonical_url or "",
                        _digest_text(description_text),
                    ]
                )
            ),
            canonical_apply_url=canonical_url,
        )

    # ---- level 3 -------------------------------------------------------------

    def _l3_hash(
        self,
        *,
        company_key: str,
        title_key: str,
        title_raw: str,
        country_code: str | None,
        state_code: str | None,
        city: str | None,
    ) -> bytes | None:
        """Company + title + location, but only when it is safe to trust.

        Returns ``None`` — meaning "do not use L3 for this job" — when a match would be
        too likely to merge distinct openings.
        """
        if not company_key or not title_key:
            return None

        # A specific place is required. Merging every "Analyst at BigCo in the US" would
        # collapse dozens of real, distinct postings into one.
        city_key = _normalize_key(city or "")
        if not city_key or not state_code:
            return None

        # Generic titles repeat legitimately within one company and city.
        if title_raw.strip().lower() in _GENERIC_TITLES or title_key in _GENERIC_TITLES:
            return None

        return _sha256("|".join([company_key, title_key, country_code or "", state_code, city_key]))

    # ---- normalizers ---------------------------------------------------------

    @staticmethod
    def canonicalize_url(url: str | None) -> str | None:
        """Reduce an apply URL to its identity.

        Lowercases scheme and host, drops tracking parameters and fragments, sorts the
        remaining query, and strips a trailing slash. Path case is preserved because many
        ATS job ids are case-sensitive — lowercasing them would merge different jobs.
        """
        if not url:
            return None
        candidate = url.strip()
        if not candidate:
            return None

        try:
            parts = urlsplit(candidate)
        except ValueError:
            return None

        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return None

        host = parts.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        # Drop default ports so :443 and implicit HTTPS agree.
        host = host.removesuffix(":443").removesuffix(":80")

        query_items = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=False)
            if key.lower() not in _TRACKING_PARAMS
        ]
        query = urlencode(sorted(query_items))

        path = parts.path.rstrip("/") or "/"

        return urlunsplit((parts.scheme.lower(), host, path, query, ""))

    @staticmethod
    def normalize_company(name: str | None) -> str:
        """Strip legal suffixes and punctuation so "ABC Corp." == "ABC Corporation"."""
        if not name:
            return ""
        without_suffix = _LEGAL_SUFFIXES.sub(" ", name.lower())
        return _normalize_key(without_suffix)

    @staticmethod
    def normalize_title(title: str | None) -> str:
        """Strip seniority markers, bracketed asides and trailing clauses.

        "Senior Software Engineer (Remote) - Platform" and "Software Engineer" collapse to
        the same key. Used only as *part* of L3, which is why the aggressive reduction is
        acceptable: the guards above stop it from over-merging on its own.
        """
        if not title:
            return ""
        reduced = _TITLE_NOISE.sub(" ", title.lower())
        normalized = _normalize_key(reduced)
        # If stripping removed everything, fall back to the raw title.
        return normalized or _normalize_key(title)


def _normalize_key(value: str) -> str:
    """Lowercase, collapse non-alphanumerics to single spaces, trim."""
    return _NON_ALNUM.sub(" ", value.lower()).strip()


def _digest_text(text: str | None, *, limit: int = 4000) -> str:
    """Stable digest of a description.

    Whitespace-normalized and truncated: descriptions carry inline base64 images and
    boilerplate that differ byte-for-byte between otherwise identical postings.
    """
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(collapsed[:limit].encode("utf-8")).hexdigest()[:32]


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()
