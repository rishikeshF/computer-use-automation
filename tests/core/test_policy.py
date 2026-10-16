"""Policy against the real policies/parabank.yaml. These are the M2 acceptance tests."""

from pathlib import Path

import pytest

from cua.core.action import Click, Fill, Navigate, Select
from cua.core.policy import Policy, PolicyConfig
from cua.surface.base import Node

POLICY = Policy.load(Path(__file__).resolve().parents[2] / "policies" / "parabank.yaml")
BASE = "https://parabank.parasoft.com/parabank"
PAGE = f"{BASE}/openaccount.htm;jsessionid=ABC123"


def control(role: str, name: str = "", label: str | None = None) -> Node:
    return Node(ref="e1", role=role, name=name, label=label, bbox=(0, 0, 1, 1))


# ---- navigation allowlist -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/admin.htm",
        f"{BASE}/ADMIN.HTM",  # case
        f"{BASE}/admin.htm;jsessionid=8F584B4C",  # session id in the path
        f"{BASE}/%61dmin.htm",  # percent-encoded
        f"{BASE}/%2561dmin.htm",  # double-encoded
        f"{BASE}/overview.htm/../admin.htm",  # dot segments
        f"{BASE}/admin.htm?x=1#top",  # query and fragment
        f"{BASE}/services/bank/customers/12212",  # the API, out of scope
        "https://evil.example/parabank/index.htm",  # other origin
        "http://parabank.parasoft.com/parabank/index.htm",  # plain http is a different origin
        "javascript:alert(1)",
        "file:///etc/passwd",
    ],
)
def test_navigation_outside_policy_is_denied(url: str) -> None:
    d = POLICY.check(Navigate(url=url), url=PAGE)
    assert d.verdict == "deny", (url, d)


def test_admin_rule_is_named_in_the_decision() -> None:
    d = POLICY.check(Navigate(url=f"{BASE}/admin.htm"), url=PAGE)
    assert d.rule == "deny_paths:/parabank/admin.htm"


@pytest.mark.parametrize("path", ["/index.htm", "/overview.htm;jsessionid=X", "/activity.htm?id=1"])
def test_navigation_inside_policy_is_allowed(path: str) -> None:
    d = POLICY.check(Navigate(url=BASE + path), url="about:blank")
    assert d.verdict == "allow" and d.risk == "read_only"


def test_admin_link_is_denied_even_with_approval() -> None:
    d = POLICY.check(
        Click(ref="e1"), url=PAGE, target=control("link", "Admin Page"), irreversible_approved=True
    )
    assert d.verdict == "deny" and d.rule.startswith("deny_controls")


def test_actions_on_a_page_outside_policy_are_denied() -> None:
    d = POLICY.check(Click(ref="e1"), url=f"{BASE}/admin.htm", target=control("link", "Home"))
    assert d.verdict == "deny"


# ---- risk ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Open New Account", "Transfer", "Send Payment", "Apply Now"])
def test_irreversible_click_without_approval_is_blocked(name: str) -> None:
    d = POLICY.check(Click(ref="e1"), url=PAGE, target=control("button", name))
    assert d.verdict == "needs_approval" and d.risk == "irreversible"
    assert not d.allowed


def test_irreversible_click_with_approval_is_allowed() -> None:
    d = POLICY.check(
        Click(ref="e1"),
        url=PAGE,
        target=control("button", "Open New Account"),
        irreversible_approved=True,
    )
    assert d.verdict == "allow" and d.risk == "irreversible"


def test_unknown_button_defaults_to_irreversible() -> None:
    d = POLICY.check(Click(ref="e1"), url=PAGE, target=control("button", "Close Account"))
    assert d.verdict == "needs_approval" and d.rule == "risk.default_button"


def test_click_on_unseen_target_is_treated_as_irreversible() -> None:
    assert POLICY.check(Click(ref="e9"), url=PAGE).verdict == "needs_approval"


def test_known_safe_controls() -> None:
    assert POLICY.check(Click(ref="e1"), url=PAGE, target=control("button", "Log In")).allowed
    link = POLICY.check(Click(ref="e1"), url=PAGE, target=control("link", "Open New Account"))
    assert link.allowed and link.risk == "read_only"  # the menu link only opens the form
    assert POLICY.check(Fill(ref="e1", value="x"), url=PAGE, target=control("textbox")).allowed
    assert POLICY.check(Select(ref="e1", option="SAVINGS"), url=PAGE).risk == "reversible"


def test_disallowed_action_kind() -> None:
    config = POLICY.config.model_copy(update={"actions": ["click"]})
    d = Policy(config).check(Navigate(url=f"{BASE}/index.htm"), url=PAGE)
    assert d.verdict == "deny" and d.rule == "actions"


def test_policy_file_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        PolicyConfig.model_validate(
            {
                "name": "x",
                "origins": ["https://a.test"],
                "allow_paths": ["/*"],
                "actions": ["click"],
                "deny_path": ["/admin"],  # typo: must not silently drop the deny rule
            }
        )


# ---- sensitive inputs ---------------------------------------------------------------------------


def test_sensitive_inputs_from_policy_and_artifact() -> None:
    inputs = {
        "username": "john",
        "password": "demo",
        "funding_account_id": "13344",
        "account_type": "SAVINGS",
        "memo": "rent",
    }
    got = POLICY.sensitive_values(inputs, declared=["memo"])
    assert set(got) == {"username", "password", "funding_account_id", "memo"}
    assert "account_type" not in got  # "account_type" is not an account number


def test_a_tenants_renamed_controls_keep_their_risk() -> None:
    """Tenant B says "Sign In" and "Open a Share Account". Same controls, same rules."""
    tenant = POLICY.renamed({"Log In": "Sign In", "Open New Account": "Open a Share Account"})
    click = Click(ref="e1")
    assert tenant.classify(click, control("button", "Sign In"))[0] == "reversible"
    assert tenant.classify(click, control("button", "Open a Share Account"))[0] == "irreversible"
    assert (
        POLICY.classify(click, control("button", "Sign In"))[0] == "irreversible"
    )  # base: unknown
