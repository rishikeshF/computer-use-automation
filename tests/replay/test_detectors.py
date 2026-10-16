"""App-level detectors, as pure functions over an Observation."""

import pytest

from cua.core.artifact import Checkpoint
from cua.core.faults import FaultPlan, parse_faults
from cua.replay.detectors import app_issue, checkpoint_gap
from cua.surface.base import Node, Observation


def obs(*nodes: tuple[str, str, tuple[str, ...]], status: int | None = 200) -> Observation:
    return Observation(
        url="https://x/parabank/overview.htm;jsessionid=AB",
        title="t",
        http_status=status,
        nodes=tuple(
            Node(ref=f"e{i}", role=r, name=n, states=st, bbox=(0, 0, 1, 1))
            for i, (r, n, st) in enumerate(nodes)
        ),
    )


LOGIN = (("textbox", "", ("secret",)), ("button", "Log In", ()))
ERROR = (("heading", "Error!", ()), ("text", "An internal error has occurred.", ()))


def test_login_form_wins_over_error_text() -> None:
    """ParaBank with no session shows both. It is an expired session, not a server fault."""
    assert app_issue(obs(*LOGIN, *ERROR)) == ("session_expired", "the login form is showing")


def test_error_page_and_http_status() -> None:
    issue = app_issue(obs(*ERROR))
    assert issue is not None and issue[0] == "app_error" and "internal error" in issue[1]
    assert app_issue(obs(status=503)) == ("app_error", "HTTP 503 on /parabank/overview.htm")


def test_session_check_can_be_switched_off_while_signing_in() -> None:
    assert app_issue(obs(*LOGIN), check_session=False) is None


def test_checkpoint_gap_explains_what_is_missing() -> None:
    cp = Checkpoint(url_pattern="/parabank/overview.htm", landmark_text="Accounts Overview")
    assert checkpoint_gap(cp, obs(("heading", "Accounts Overview", ())), {}) is None
    assert checkpoint_gap(cp, obs(), {}) == "expected 'Accounts Overview' on the page, not there"


def test_fault_flags() -> None:
    assert parse_faults(["http500:2", "slow", "session_expire"]) == FaultPlan(
        http500_times=2, slow_ms=1500, session_expire=True
    )
    with pytest.raises(ValueError, match="unknown fault"):
        parse_faults(["meteor"])
