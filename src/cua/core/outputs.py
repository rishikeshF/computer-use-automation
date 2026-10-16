"""Typed outputs: turn what the screen shows into a value a calling agent can compute with.

The UI shows "$515.50". An agent asking "is the balance above 500?" needs Decimal("515.50").
The type is decided once, at record time, from the value the run actually read, and written into
the artifact. Replay converts with the declared type and fails loudly if the text no longer fits:
a balance that suddenly reads "N/A" is a finding, not something to pass on silently.

The raw text is never thrown away. It stays in the run's evidence next to the typed value.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

OutputType = Literal["string", "decimal"]

# "$1,234.56", "-$10.00", "$-10.00", "($10.00)", "1234.56". Two decimals: money, not a count or id.
_MONEY = re.compile(
    r"""^(?P<open>\()?                      # "($10.00)" is how ledgers show a negative
        (?P<sign>-)?\$?(?P<sign2>-)?          # "-$10.00" or "$-10.00"
        (?P<num>\d{1,3}(?:,\d{3})*|\d+)      # "1,234" or "1234"
        \.(?P<cents>\d{2})
        (?P<close>\))?$""",
    re.VERBOSE,
)


def infer_type(raw: str) -> tuple[OutputType, str | None]:
    """(type, unit) for a value as read. Unit is "USD" when the text shows a dollar sign."""
    text = raw.strip()
    if _parse_money(text) is not None:
        return "decimal", "USD" if "$" in text else None
    return "string", None


def convert(raw: str, type_: OutputType) -> str | Decimal:
    """The typed value. Raises ValueError when the text does not fit the declared type."""
    if type_ == "string":
        return raw
    value = _parse_money(raw.strip())
    if value is None:
        raise ValueError(f"expected a decimal amount, got {raw!r}")
    return value


def _parse_money(text: str) -> Decimal | None:
    m = _MONEY.match(text)
    if not m or bool(m["open"]) != bool(m["close"]) or (m["sign"] and m["sign2"]):
        return None
    value = Decimal(f"{m['num'].replace(',', '')}.{m['cents']}")
    negative = bool(m["open"] or m["sign"] or m["sign2"])
    return -value if negative else value
