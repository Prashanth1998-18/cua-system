"""
The write-up has to agree with the code.

A reviewer asked where we use the Anthropic SDK. We don't, and never did:
an ADR had said so and was never corrected after the decision changed.
Auditing the rest turned up a stale test count, a stale ADR status, three
wrong cross-references, and a "~50-line loop" that had grown to ~470.

Every one of those was mechanically checkable. So they are checked here:
the numbers the docs claim, and the paths they point at. Prose can still
rot, but a claim with a number in it can't do so quietly.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
REPORT = (ROOT / "REPORT.md").read_text()
DECISIONS = (ROOT / "decisions.md").read_text()


def _claimed(pattern: str, text: str, label: str) -> int:
    """The number a document claims, so a mismatch names what it read."""
    found = re.findall(pattern, text)
    assert found, f"no claim matching {label} found — did the wording change?"
    return int(found[0].replace(",", ""))


def test_readme_test_count_is_the_real_one():
    """
    Adding a test fails this until the README is updated. That friction is
    the point: it is the only thing that keeps the number true.
    """
    claimed = _claimed(r"#\s*([\d,]+) tests", README, "'# N tests' in the README")

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(ROOT / "tests")],
        capture_output=True, text=True, cwd=ROOT,
    )
    actual = int(re.search(r"(\d+) tests collected", collected.stdout).group(1))

    assert claimed == actual, (
        f"README claims {claimed} tests, pytest collects {actual}. "
        f"Update the README rather than deleting this test."
    )


def test_adr_count_is_the_real_one():
    real = len(re.findall(r"^## ADR-", DECISIONS, re.M))
    for name, text in (("README", README), ("REPORT", REPORT)):
        claimed = _claimed(r"(\d+) ADRs", text, f"'N ADRs' in the {name}")
        assert claimed == real, f"{name} claims {claimed} ADRs, decisions.md has {real}"


def test_zip_code_count_is_the_real_one():
    claimed = _claimed(r"([\d,]+)-entry", REPORT, "'N-entry' zip list in the REPORT")
    lines = (ROOT / "cua" / "data" / "us_zip_codes.txt").read_text().splitlines()
    real = len([line for line in lines if line and not line.startswith("#")])
    assert claimed == real, f"REPORT claims {claimed} zip codes, the file has {real}"


@pytest.mark.parametrize("document", ["README.md", "REPORT.md"])
def test_every_path_the_docs_point_at_exists(document):
    """
    Broken references are the quietest kind of wrong: they read fine and
    they send a reviewer nowhere.
    """
    text = (ROOT / document).read_text()
    referenced = set(re.findall(r"`((?:evidence|cua|scripts|tests)/[A-Za-z0-9_./*-]+)`", text))
    assert referenced, f"{document} points at no paths — did the formatting change?"

    missing = []
    for path in sorted(referenced):
        if "*" in path:
            if not list(ROOT.glob(path)):
                missing.append(f"{path} (glob matches nothing)")
        elif not (ROOT / path).exists():
            missing.append(path)

    assert not missing, f"{document} points at paths that don't exist: {missing}"
