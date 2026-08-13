"""Source connectors.

Provider-specific knowledge lives ONLY in the per-source subpackages here. See
scripts/check_layering.py, which fails CI if it leaks elsewhere.
"""

from .base import (
    ConnectorCapabilities,
    NormalizedJob,
    RawRecord,
    SourceConnector,
    SourceFile,
    ValidationResult,
)

__all__ = [
    "ConnectorCapabilities",
    "NormalizedJob",
    "RawRecord",
    "SourceConnector",
    "SourceFile",
    "ValidationResult",
]
