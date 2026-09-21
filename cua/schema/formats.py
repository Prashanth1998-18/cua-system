"""
Input formats: rules an input must follow that the target app can't reveal
(e.g. SauceDemo accepts any zip and silently bounces a bad one back to the
cart). The discovery agent records a format when the task states the rule
(ADR-016); replay checks it before touching the browser.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Callable

_DATA = Path(__file__).resolve().parent.parent / "data"


@cache
def _us_zip_codes() -> frozenset[str]:
    lines = (_DATA / "us_zip_codes.txt").read_text().splitlines()
    return frozenset(line for line in lines if line and not line.startswith("#"))


def _is_us_zip_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{5}", value)) and value in _us_zip_codes()


# name -> (check, what a valid value looks like, for error messages)
FORMATS: dict[str, tuple[Callable[[str], bool], str]] = {
    "us_zip_code": (_is_us_zip_code, "an existing 5-digit US zip code"),
}
