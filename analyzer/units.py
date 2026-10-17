"""Classification of declared currency-amount semantics (minor vs major units).

Used to interpret explicit units_changed evidence. The demo is USD-only; the
minor-to-major factor is only known for currencies listed here, and anything else
stays unknown rather than guessed.
"""

from __future__ import annotations

import re
from typing import Literal

AmountUnit = Literal["minor", "major"]

# Minor units per major unit, for currencies this MVP makes claims about.
MINOR_UNITS_PER_MAJOR = {"usd": 100}

_MINOR_PATTERNS = (
    r"\bminor\b",
    r"\bcents?\b",
    r"\bsmallest currency unit\b",
    r"\blowest (?:currency )?denomination\b",
)
_MAJOR_PATTERNS = (
    r"\bmajor\b",
    r"\bdollars?\b",
    r"\bwhole (?:currency )?units?\b",
)
_CURRENCY_PATTERN = re.compile(r"\b(usd|eur|gbp|jpy|cad|aud)\b", re.IGNORECASE)


def classify_amount_unit(text: str | None) -> AmountUnit | None:
    """Return "minor"/"major" when the text says so unambiguously, else None."""
    if not text:
        return None
    lowered = text.lower()
    minor = any(re.search(pattern, lowered) for pattern in _MINOR_PATTERNS)
    major = any(re.search(pattern, lowered) for pattern in _MAJOR_PATTERNS)
    if minor == major:  # neither, or contradictory
        return None
    return "minor" if minor else "major"


def currency_of(*texts: str | None) -> str | None:
    """The single ISO currency code mentioned across ``texts`` (lowercase), else None."""
    found = {match.lower() for text in texts if text for match in _CURRENCY_PATTERN.findall(text)}
    return found.pop() if len(found) == 1 else None


def minor_to_major_factor(currency: str | None) -> int | None:
    return MINOR_UNITS_PER_MAJOR.get(currency or "")
