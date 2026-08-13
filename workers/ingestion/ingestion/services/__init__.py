"""Cross-cutting pipeline services.

These apply one consistent policy to every source. A connector reports what its source
said; these decide what it means.
"""

from .dedupe import DedupeKeys, DedupeService
from .freshness import FreshnessAssessment, FreshnessService
from .location import LocationNormalizer, ResolvedLocation

__all__ = [
    "DedupeKeys",
    "DedupeService",
    "FreshnessAssessment",
    "FreshnessService",
    "LocationNormalizer",
    "ResolvedLocation",
]
