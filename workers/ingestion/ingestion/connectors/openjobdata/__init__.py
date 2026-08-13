"""OpenJobData connector.

Everything this platform knows about OpenJobData lives in this package. Nothing outside
it may reference the provider -- see scripts/check_layering.py, which fails CI otherwise.

Source facts are documented and re-verifiable: docs/00-source-verification.md, and
`python scripts/verify_source.py`.
"""

from .connector import OpenJobDataConnector
from .schema import (
    JOB_COLUMNS_FULL,
    JOB_COLUMNS_MINIMAL,
    SOURCE_NAME,
    OpenJobDataPaths,
    decode_nested_json,
)

__all__ = [
    "JOB_COLUMNS_FULL",
    "JOB_COLUMNS_MINIMAL",
    "SOURCE_NAME",
    "OpenJobDataConnector",
    "OpenJobDataPaths",
    "decode_nested_json",
]
