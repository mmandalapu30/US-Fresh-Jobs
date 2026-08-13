#!/usr/bin/env python
"""Fail the build if provider-specific knowledge leaks out of its connector package.

The specification requires that OpenJobData logic must not be hard-coded throughout the
application, because Greenhouse, Ashby, Lever, Jobven and employer career sites are coming
later. A code-review convention is not enough for that -- it decays. This check makes the
rule mechanical.

Rule
----
Provider tokens (``openjobdata``, ``huggingface``, ``hf://``, ...) may appear ONLY in:

* that provider's connector package, e.g. ``workers/ingestion/ingestion/connectors/openjobdata/``
* its own tests
* configuration, documentation and scripts (where naming the source is the entire point)

Anywhere else -- API routers, repositories, pipeline stages, the frontend -- is a
violation, because it means the platform has learned something about a specific source
that it should have learned through the ``SourceConnector`` interface.

Usage
-----
    python scripts/check_layering.py           # exit 1 on violation
    python scripts/check_layering.py --list    # show the rules and exit
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProviderRule:
    """One provider and the only places it is allowed to be mentioned."""

    name: str
    #: Case-insensitive substrings that betray provider-specific knowledge.
    tokens: tuple[str, ...]
    #: Path prefixes (repo-relative, POSIX) where those tokens are legitimate.
    allowed_prefixes: tuple[str, ...]


PROVIDER_RULES: tuple[ProviderRule, ...] = (
    ProviderRule(
        name="openjobdata",
        tokens=("openjobdata", "huggingface", "hf://", "huggingface_hub", "invicto69"),
        allowed_prefixes=(
            "workers/ingestion/ingestion/connectors/openjobdata/",
            "workers/ingestion/tests/connectors/openjobdata/",
            "workers/ingestion/tests/test_openjobdata",
        ),
    ),
)

#: Areas where naming a source is the point rather than a leak.
GLOBAL_EXEMPT_PREFIXES: tuple[str, ...] = (
    "docs/",
    "scripts/",
    ".github/",
    "infra/",
    "README.md",
    ".env",
    "conftest.py",
    # Settings must name the sources they configure; that is configuration, not logic.
    # The settings test is exempt for the same reason: it asserts those config defaults.
    "packages/shared/jobplatform_shared/config.py",
    "tests/test_config.py",
    # The connector package docstring explains the rule and cites the provider name.
    "workers/ingestion/ingestion/connectors/__init__.py",
    "workers/ingestion/ingestion/connectors/base.py",
    # Compose files wire the services together by name.
    "docker-compose",
    "Makefile",
    # The guard's own test asserts on the rule output, so it must name the token.
    "workers/ingestion/tests/test_layering.py",
)

SCANNED_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".sql"}

SKIP_DIR_PARTS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".next",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
}


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(part in SKIP_DIR_PARTS for part in path.parts):
            continue
        if path.name.endswith(".egg-info") or ".egg-info" in str(path):
            continue
        files.append(path)
    return files


def _is_exempt(rel_path: str, rule: ProviderRule) -> bool:
    if rel_path.startswith(GLOBAL_EXEMPT_PREFIXES):
        return True
    return rel_path.startswith(rule.allowed_prefixes)


def check() -> list[str]:
    """Return a list of human-readable violations."""
    violations: list[str] = []

    for path in _iter_source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for rule in PROVIDER_RULES:
            if _is_exempt(rel, rule):
                continue
            for token in rule.tokens:
                pattern = re.compile(re.escape(token), re.IGNORECASE)
                for lineno, line in enumerate(content.splitlines(), start=1):
                    if pattern.search(line):
                        violations.append(
                            f"{rel}:{lineno}: provider token {token!r} outside "
                            f"{rule.allowed_prefixes[0]}\n"
                            f"    {line.strip()[:110]}"
                        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the rules and exit")
    args = parser.parse_args()

    if args.list:
        print("Layering rules:\n")
        for rule in PROVIDER_RULES:
            print(f"  provider: {rule.name}")
            print(f"    tokens : {', '.join(rule.tokens)}")
            print("    allowed:")
            for prefix in rule.allowed_prefixes:
                print(f"      - {prefix}")
        print("\n  globally exempt:")
        for prefix in GLOBAL_EXEMPT_PREFIXES:
            print(f"      - {prefix}")
        return 0

    violations = check()
    if violations:
        print("Layering violations -- provider knowledge has leaked out of its connector:\n")
        for violation in violations:
            print(f"  {violation}")
        print(
            f"\n{len(violations)} violation(s).\n"
            "Route this through the SourceConnector interface instead, or add a deliberate\n"
            "exemption in scripts/check_layering.py with a reason."
        )
        return 1

    scanned = len(_iter_source_files())
    print(f"Layering OK -- {scanned} files scanned, no provider knowledge outside its connector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
