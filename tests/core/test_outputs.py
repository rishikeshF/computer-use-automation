"""Typed outputs: what the screen shows -> a value an agent can compute with."""

from decimal import Decimal

import pytest

from cua.core.outputs import convert, infer_type


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("$515.50", Decimal("515.50")),
        ("-$10.00", Decimal("-10.00")),
        ("$-1,234.56", Decimal("-1234.56")),
        ("($10.00)", Decimal("-10.00")),  # ledger-style negative
        ("1234.56", Decimal("1234.56")),
    ],
)
def test_amounts_are_decimal(raw: str, value: Decimal) -> None:
    assert infer_type(raw)[0] == "decimal"
    assert convert(raw, "decimal") == value


def test_unit_comes_from_the_currency_sign() -> None:
    assert infer_type("$515.50") == ("decimal", "USD")
    assert infer_type("515.50") == ("decimal", None)


@pytest.mark.parametrize("raw", ["49974", "$5", "N/A", "($1.00", "-$-1.00", "1,23.00"])
def test_ids_and_odd_text_stay_strings(raw: str) -> None:
    assert infer_type(raw) == ("string", None)


def test_declared_decimal_that_no_longer_fits_is_an_error() -> None:
    with pytest.raises(ValueError, match="expected a decimal"):
        convert("N/A", "decimal")
