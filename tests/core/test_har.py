"""Sanitizing a recording for offline mode. Pure JSON, no browser."""

import json

from cua.core.har import leaks, sanitize, stand_ins

SECRETS = {"username": "rishi", "password": "p@ss word!", "account_id": "49974"}


def recording() -> dict[str, object]:
    """The parts of a real ParaBank recording that carry secrets."""
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://x/parabank/login.htm;jsessionid=8F58A0C1",
                        "headers": [{"name": "Cookie", "value": "JSESSIONID=8F58A0C1"}],
                        "postData": {"text": "username=rishi&password=p%40ss+word%21"},
                    },
                    "response": {
                        "headers": [
                            {"name": "Content-Length", "value": "1234"},
                            {"name": "Location", "value": "overview.htm;jsessionid=8F58A0C1"},
                        ],
                        "content": {"mimeType": "text/html", "text": "<a>49974</a> $515.50"},
                    },
                },
                {
                    "request": {"method": "GET", "url": "https://x/parabank/img/logo.gif"},
                    "response": {"content": {"encoding": "base64", "text": "rishi49974=="}},
                },
            ]
        }
    }


def test_stand_ins_keep_the_shape_and_avoid_collisions() -> None:
    fakes = stand_ins(SECRETS, taken="... 10000 ...")
    assert fakes["username"] == "demo-username" and fakes["password"] == "demo-password"
    assert fakes["account_id"].isdigit() and len(fakes["account_id"]) == 5
    assert fakes["account_id"] != "10000"  # already on a page: would be ambiguous


def test_every_secret_is_replaced_consistently() -> None:
    fakes = stand_ins(SECRETS, taken="")
    clean = sanitize(recording(), {SECRETS[k]: fakes[k] for k in SECRETS})
    assert leaks(clean, SECRETS.values()) == []
    body = json.dumps(clean)
    first = clean["log"]["entries"][0]
    # The form post holds the stand-ins, so the offline run (typing them) matches it.
    assert first["request"]["postData"]["text"] == (
        f"username={fakes['username']}&password={fakes['password']}"
    )
    assert f"<a>{fakes['account_id']}</a> $515.50" in body
    # Session ids are one constant everywhere: URLs and redirects still line up.
    assert "8F58A0C1" not in body and body.count("jsessionid=OFFLINE") == 2
    assert [h["name"] for h in first["response"]["headers"]] == ["Location"]


def test_binary_bodies_are_left_alone() -> None:
    """Base64 is bytes, not text. A match inside it is a coincidence; rewriting it would break
    the image. It cannot leak a secret as text either."""
    clean = sanitize(recording(), {"rishi": "demo"})
    assert clean["log"]["entries"][1]["response"]["content"]["text"] == "rishi49974=="


def test_leaks_finds_url_encoded_values() -> None:
    raw = {"postData": "password=p%40ss+word%21"}
    assert leaks(raw, ["p@ss word!"]) == ["p@ss word!"]
