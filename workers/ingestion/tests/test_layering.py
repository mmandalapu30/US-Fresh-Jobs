"""The layering guard is itself tested.

A guard that silently stops working is worse than no guard: the build stays green while
the rule it protects decays. These tests prove it still detects a real violation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_layering.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    # S603: the command is this interpreter running a checked-in script with literal
    # arguments. No part of it comes from input.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )


def test_repository_currently_passes() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Layering OK" in result.stdout


def test_rules_can_be_listed() -> None:
    result = _run("--list")
    assert result.returncode == 0
    assert "openjobdata" in result.stdout


def test_guard_detects_an_injected_violation(tmp_path: Path) -> None:
    """Plant a leak in a non-exempt location and confirm the guard fails the build."""
    from check_layering import check

    planted = REPO_ROOT / "workers" / "ingestion" / "ingestion" / "pipeline" / "_leak_probe.py"
    planted.write_text(
        '"""Temporary probe."""\n'
        "BUCKET = 'hf://buckets/example/thing'  # provider knowledge in the wrong layer\n",
        encoding="utf-8",
    )
    try:
        violations = check()
        assert any("_leak_probe.py" in v for v in violations), (
            "the guard failed to detect a planted provider token"
        )
    finally:
        planted.unlink(missing_ok=True)

    # And the repository is clean again once the probe is gone.
    assert not any("_leak_probe.py" in v for v in check())
