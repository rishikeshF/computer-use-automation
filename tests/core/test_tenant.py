"""Tenant overrides as pure functions. No browser."""

import pytest

from cua.core.locator import RoleName, TableCell
from cua.core.tenant import OverrideError, TenantOverride, apply, start_path
from tests.fakebank import BALANCE, SESSION

TENANT_B = TenantOverride(
    tenant="tenant_b",
    vendor="test",
    text={
        "Accounts Overview": "Account Summary",
        "Balance*": "Current Balance*",
        "Not used by this capability": "Fine",
    },
    signals={"login_button": "Sign In"},
    fingerprints={"/overview.htm": "nav:tenantb"},
)


def test_override_derives_the_tenants_artifact_and_leaves_the_base_alone() -> None:
    effective, renamed = apply(BALANCE, TENANT_B)
    assert effective.id == "get_account_balance@0.1.0+tenant_b"
    assert effective.overrides == {
        "tenant": "tenant_b",
        "base": BALANCE.id,
        "base_hash": BALANCE.content_hash,
    }
    assert effective.content_hash == effective.behavior_hash() != BALANCE.content_hash
    assert renamed == ["Accounts Overview -> Account Summary", "Balance* -> Current Balance*"]
    s1, s2 = effective.steps[0], effective.steps[1]
    assert s1.post.landmark_text == "Account Summary"
    assert s2.target is not None
    # The column is renamed; the row is still the caller's input, untouched.
    assert s2.target.strategies[0] == TableCell(
        role="cell", row="{{account_id}}", col="Current Balance*"
    )
    guards = effective.outcomes[0].when.present
    assert guards[1].strategies[0] == RoleName(role="columnheader", name="Current Balance*")
    assert effective.app.app_version_fingerprint == "nav:tenantb"
    assert BALANCE.steps[0].post.landmark_text == "Accounts Overview"  # base unchanged
    assert effective.problems() == []


def test_override_for_another_product_is_refused() -> None:
    other = TENANT_B.model_copy(update={"vendor": "acme/core"})
    with pytest.raises(OverrideError, match="not test"):
        apply(BALANCE, other)


def test_fingerprint_is_the_one_for_the_artifacts_own_start_page() -> None:
    assert start_path(BALANCE) == "/overview.htm"
    assert start_path(SESSION) == "/index.htm"
    # Sign-in opens on another page. The overview's fingerprint would be a false alarm there,
    # so with no entry for its page there is no fingerprint to check.
    session, _ = apply(SESSION, TENANT_B)
    assert session.app.app_version_fingerprint is None
