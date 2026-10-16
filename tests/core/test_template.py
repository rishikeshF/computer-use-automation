"""{{param}} placeholders and URL patterns: how artifacts refer to caller values."""

import pytest

from cua.core.locator import LocatorChain, RoleName, TableCell
from cua.core.template import placeholders, render, templatize
from cua.core.url import url_matches, url_pattern

PARAMS = {"account_id": "13344"}


def test_render_and_templatize_round_trip() -> None:
    assert templatize("Account 13344 details", PARAMS) == "Account {{account_id}} details"
    assert render("Account {{account_id}} details", PARAMS) == "Account 13344 details"
    assert placeholders("{{a}} and {{ b }}") == {"a", "b"}


def test_templatize_only_replaces_whole_tokens() -> None:
    # 133 is a param, but it must not be cut out of a bigger number.
    assert templatize("$1336.00 in 133", {"short": "133"}) == "$1336.00 in {{short}}"


def test_render_refuses_missing_param() -> None:
    with pytest.raises(KeyError):
        render("{{missing}}", PARAMS)


def test_locator_chain_bind_fills_every_strategy() -> None:
    chain = LocatorChain(
        strategies=(
            RoleName(role="link", name="{{account_id}}"),
            TableCell(role="cell", row="{{account_id}}", col="Balance"),
        )
    )
    bound = chain.bind(PARAMS)
    assert bound.strategies[0] == RoleName(role="link", name="13344")
    assert bound.strategies[1] == TableCell(role="cell", row="13344", col="Balance")


def test_url_pattern_parameterizes_and_wildcards() -> None:
    url = "https://x/parabank/activity.htm;jsessionid=ABC?id=13344&ts=99"
    pattern = url_pattern(url, PARAMS)
    assert pattern == "/parabank/activity.htm?id={{account_id}}&ts=*"
    assert url_matches("https://x/parabank/activity.htm?ts=1&id=13344", pattern, PARAMS)
    assert not url_matches("https://x/parabank/activity.htm?id=99999&ts=1", pattern, PARAMS)
    assert not url_matches("https://x/parabank/overview.htm?id=13344&ts=1", pattern, PARAMS)
