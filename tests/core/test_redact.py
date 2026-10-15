"""Redaction: known secrets by name, then sensitive shapes."""

from cua.core.action import Fill
from cua.core.redact import Redactor, redact


def test_known_secrets_are_replaced_by_name() -> None:
    r = Redactor({"password": "hunter2", "account_id": "13344"})
    assert r("login with hunter2 for 13344") == (
        "login with [REDACTED:password] for [REDACTED:account_id]"
    )


def test_secret_is_matched_on_boundaries_only() -> None:
    r = Redactor({"pin": "42"})
    assert r("pin 42, total 1420") == "pin [REDACTED:pin], total 1420"


def test_longer_secret_wins_over_contained_one() -> None:
    r = Redactor({"short": "abc", "long": "abc-def"})
    assert r("abc-def") == "[REDACTED:long]"


def test_session_ids_and_tokens() -> None:
    url = "https://parabank.parasoft.com/parabank/overview.htm;jsessionid=8F584B4C34830E91"
    assert redact(url).endswith("overview.htm;jsessionid=[REDACTED]")
    assert redact("Cookie: JSESSIONID=8F584B4C") == "Cookie: [REDACTED]"  # whole cookie value
    assert redact("JSESSIONID=8F584B4C; Path=/") == "JSESSIONID=[REDACTED]; Path=/"
    assert redact("password=hunter2&user=x") == "password=[REDACTED]&user=x"
    # The whole header value goes, not just its first word ("Bearer").
    assert redact("Authorization: Bearer abc.def.ghi") == "Authorization: [REDACTED]"
    assert redact("sent Bearer abc.def.ghi") == "sent Bearer [REDACTED]"
    assert redact("key sk-ant-api03-abcdefghijklmnop") == "key [REDACTED:api_key]"


def test_pii_shapes() -> None:
    assert redact("SSN 123-45-6789") == "SSN [SSN]"
    assert redact("mail john.smith@bank.com now") == "mail [EMAIL] now"
    assert redact("account 4111111111111111") == "account ************1111"
    assert redact("account 13344") == "account *3344"


def test_amounts_dates_and_short_numbers_are_kept() -> None:
    s = "balance $12345.67 on 10/14/2026, 1,234,567.00 total, step 12, ref e76"
    assert redact(s) == s


def test_structured_data_by_key_and_value() -> None:
    r = Redactor({"account_id": "13344"})
    out = r.data(
        {"password": "x", "inputs": {"account_id": "13344"}, "url": "/a.htm;jsessionid=Z", "n": 3}
    )
    assert out == {
        "password": "[REDACTED]",
        "inputs": {"account_id": "[REDACTED:account_id]"},
        "url": "/a.htm;jsessionid=[REDACTED]",
        "n": 3,
    }


def test_pydantic_models_are_dumped_then_redacted() -> None:
    action = Fill(ref="e1", value="hunter2", sensitive=True)
    assert Redactor({"password": "hunter2"}).data(action)["value"] == "[REDACTED:password]"
