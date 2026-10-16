"""Replay end to end on the fake bank (tests/fakebank.py), with faults. No model, no network."""

import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.async_api import Browser, async_playwright

from cua.core.action import Action
from cua.core.artifact import Capability
from cua.core.faults import FaultPlan
from cua.core.result import BusinessOutcome, Failure, NeedsHuman, RunResult, Success
from cua.core.runlog import RunLog
from cua.replay.engine import ReplayOptions, replay
from cua.surface.faults import WebFaults
from tests.fakebank import (
    BAD,
    BALANCE,
    BASE,
    CREDS,
    GOOD,
    NEW_ID,
    OPEN,
    OPEN_INPUTS,
    OPEN_THEN_OVERVIEW,
    SESSION,
    Bank,
    Flaky,
    open_session,
    replay_in,
)


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


async def run(
    browser: Browser,
    tmp_path: Path,
    account: str,
    *,
    bank: Bank | None = None,
    faults: FaultPlan | None = None,
    cap: Capability = BALANCE,
    creds: dict[str, str] = CREDS,
) -> tuple[RunResult, RunLog, WebFaults | None]:
    s = await open_session(
        browser, tmp_path, bank or Bank(), sensitive={"account_id": account}, faults=faults
    )
    try:
        result = await replay_in(s, cap, {"account_id": account}, creds=creds)
    finally:
        await s.web.page.context.close()
    return result, s.log, s.faults


# ---- the four M4 scenarios ---------------------------------------------------------------------


async def test_success_returns_typed_outputs(browser: Browser, tmp_path: Path) -> None:
    result, log, _ = await run(browser, tmp_path, GOOD)
    assert isinstance(result, Success), result
    assert result.outputs == {"balance": Decimal("515.50"), "available": Decimal("500.00")}
    assert result.evidence.raw_outputs == {"balance": "$515.50", "available": "$500.00"}
    assert result.evidence.recoveries == ()
    log_text = (log.dir / "log.jsonl").read_text(encoding="utf-8")
    assert GOOD not in log_text and CREDS["password"] not in log_text


async def test_bad_account_is_a_business_outcome(browser: Browser, tmp_path: Path) -> None:
    result, _, _ = await run(browser, tmp_path, BAD)
    assert isinstance(result, BusinessOutcome), result
    assert (result.code, result.step_id) == ("account_not_found", "s2")


async def test_injected_500_is_retried(browser: Browser, tmp_path: Path) -> None:
    result, _, faults = await run(browser, tmp_path, GOOD, faults=FaultPlan(http500_times=1))
    assert isinstance(result, Success), result
    assert faults is not None and faults.injected == ["http500 on /bank/overview.htm"]
    (note,) = result.evidence.recoveries
    assert note.startswith("s1: HTTP 500 on /bank/overview.htm; retry 1 of 2")


async def test_persistent_500_is_an_app_error(browser: Browser, tmp_path: Path) -> None:
    result, _, _ = await run(browser, tmp_path, GOOD, faults=FaultPlan(http500_times=5))
    assert isinstance(result, Failure) and result.kind == "app_error", result
    assert result.step_id == "s1" and len(result.evidence.recoveries) == 2


async def test_session_expiry_is_recovered_via_ensure_session(
    browser: Browser, tmp_path: Path
) -> None:
    result, _, _ = await run(browser, tmp_path, GOOD, faults=FaultPlan(session_expire=True))
    assert isinstance(result, Success), result
    (note,) = result.evidence.recoveries
    assert note == "s1: session expired; signed in again, resumed at s1"


# ---- loading, layout drift, dialogs -------------------------------------------------------------


async def test_rows_loading_late_are_waited_for(browser: Browser, tmp_path: Path) -> None:
    """The table frame and headers are there at once, the rows 1.5 s later. A guard on headers
    alone would call a real account "not found" here; the data-cell guard waits."""
    result, _, _ = await run(browser, tmp_path, GOOD, bank=Bank(rows_after_ms=1500))
    assert isinstance(result, Success), result
    assert result.outputs["balance"] == Decimal("515.50")


async def test_bad_account_after_late_rows_is_still_not_found(
    browser: Browser, tmp_path: Path
) -> None:
    result, _, _ = await run(browser, tmp_path, BAD, bank=Bank(rows_after_ms=1500))
    assert isinstance(result, BusinessOutcome) and result.code == "account_not_found"


async def test_rows_that_never_load_are_a_failure_not_an_outcome(
    browser: Browser, tmp_path: Path
) -> None:
    result, _, _ = await run(browser, tmp_path, BAD, bank=Bank(rows_after_ms=-1))
    assert isinstance(result, Failure) and result.kind == "locator_not_found", result


async def test_renamed_column_is_a_failure_not_account_not_found(
    browser: Browser, tmp_path: Path
) -> None:
    """Another tenant calls the column "Balance". The guard fails, so this is a Failure for a
    person to look at, never a false business answer."""
    result, _, _ = await run(browser, tmp_path, GOOD, bank=Bank(balance_header="Balance"))
    assert isinstance(result, Failure) and result.kind == "locator_not_found", result
    assert result.step_id == "s2"


async def test_unexpected_dialog_is_dismissed_and_noted(browser: Browser, tmp_path: Path) -> None:
    result, _, _ = await run(browser, tmp_path, GOOD, faults=FaultPlan(dialog=True))
    assert isinstance(result, Success), result
    assert any("dismissed unexpected dialog" in r for r in result.evidence.recoveries)


async def test_inputs_are_checked_before_anything_runs(browser: Browser, tmp_path: Path) -> None:
    result, _, _ = await run(browser, tmp_path, "")
    assert isinstance(result, Failure) and result.kind == "invalid_input"


# ---- evidence and review status ----------------------------------------------------------------


async def test_evidence_records_which_artifact_ran_and_its_status(
    browser: Browser, tmp_path: Path
) -> None:
    result, log, _ = await run(browser, tmp_path, GOOD)
    ref = result.evidence.artifact
    assert ref is not None
    assert (ref.id, ref.status, ref.content_hash) == (BALANCE.id, "draft", BALANCE.content_hash)
    start = json.loads((log.dir / "log.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert start["event"] == "replay_start" and start["status"] == "draft"


async def test_deprecated_artifact_is_refused(browser: Browser, tmp_path: Path) -> None:
    old = BALANCE.model_copy(update={"status": "deprecated", "status_note": "superseded by 0.2.0"})
    assert old.behavior_hash() == BALANCE.content_hash  # status is review metadata, not behavior
    result, _, _ = await run(browser, tmp_path, GOOD, cap=old)
    assert isinstance(result, Failure) and result.kind == "policy_violation", result
    assert result.observed is not None and "superseded by 0.2.0" in result.observed


# ---- invalid credentials -----------------------------------------------------------------------

WRONG = {"username": "mallory", "password": "nope"}


async def test_wrong_password_is_invalid_credentials_not_an_app_error(
    browser: Browser, tmp_path: Path
) -> None:
    """The bank answers under an "Error!" heading. The declared outcome is checked first, so this
    is a business answer, not a server fault."""
    s = await open_session(browser, tmp_path, Bank(), sensitive={})
    try:
        result = await replay(SESSION, WRONG, s.surface, s.log, ReplayOptions(base_url=BASE))
    finally:
        await s.web.page.context.close()
    assert isinstance(result, BusinessOutcome), result
    assert (result.code, result.step_id) == ("invalid_credentials", "s4")


async def test_invalid_credentials_in_a_precondition_is_the_callers_answer(
    browser: Browser, tmp_path: Path
) -> None:
    """The test user is gone after a database reset: a real business outcome, not a bug."""
    result, _, _ = await run(browser, tmp_path, GOOD, creds=WRONG)
    assert isinstance(result, BusinessOutcome), result
    assert (result.code, result.step_id) == ("invalid_credentials", "ensure_session:s4")


# ---- after an irreversible step: no rewind, no retry, no re-click ------------------------------


async def open_account(
    browser: Browser, tmp_path: Path, bank: Bank, *, faults: FaultPlan | None = None
) -> tuple[RunResult, WebFaults | None]:
    """Run an approved open-account capability with confirm=true: no person involved."""
    cap = OPEN_THEN_OVERVIEW if faults else OPEN
    approved = cap.model_copy(update={"status": "approved"})
    s = await open_session(
        browser, tmp_path, bank, sensitive={"funding_account_id": GOOD}, faults=faults
    )
    try:
        return await replay_in(s, approved, OPEN_INPUTS, confirm=True), s.faults
    finally:
        await s.web.page.context.close()


async def test_approved_irreversible_capability_runs_once(browser: Browser, tmp_path: Path) -> None:
    bank = Bank()
    result, _ = await open_account(browser, tmp_path, bank)
    assert isinstance(result, Success), result
    assert result.outputs == {"new_account_id": NEW_ID}
    assert bank.creates == 1


async def test_draft_irreversible_capability_is_refused_without_a_person(
    browser: Browser, tmp_path: Path
) -> None:
    bank = Bank()
    s = await open_session(browser, tmp_path, bank, sensitive={})
    try:
        result = await replay_in(s, OPEN, OPEN_INPUTS, confirm=True)  # confirm needs approved
    finally:
        await s.web.page.context.close()
    assert isinstance(result, Failure) and result.kind == "policy_violation", result
    assert bank.creates == 0


async def test_session_lost_after_commit_needs_a_human_and_never_reclicks(
    browser: Browser, tmp_path: Path
) -> None:
    """The session dies right after the account is opened. Before an irreversible step the engine
    would sign in again and rewind to the last page load (s1), which would open a second
    account. After it, the run stops and asks."""
    bank = Bank(expire_on_create=True)
    result, _ = await open_account(browser, tmp_path, bank)
    assert isinstance(result, NeedsHuman), result
    assert result.step_id == "s4"
    assert "session expired after irreversible step s4" in result.reason
    assert bank.creates == 1
    assert result.evidence.recoveries == ()  # no re-login was attempted


async def test_failed_page_load_after_commit_is_not_retried(
    browser: Browser, tmp_path: Path
) -> None:
    bank = Bank()
    faults = FaultPlan(http500_times=1, http500_path="/bank/overview.htm")
    result, injector = await open_account(browser, tmp_path, bank, faults=faults)
    assert isinstance(result, NeedsHuman), result
    assert result.step_id == "s6" and "not retrying" in result.reason
    assert injector is not None and len(injector.injected) == 1  # one load, no retry
    assert bank.creates == 1


# ---- the surface breaks mid-action -------------------------------------------------------------


def nth(kind: str, n: int) -> Callable[[Action], bool]:
    """Matches the n-th action of this kind (1-based) the run performs."""
    seen = 0

    def match(action: Action) -> bool:
        nonlocal seen
        if action.kind != kind:
            return False
        seen += 1
        return seen == n

    return match


async def test_surface_error_on_a_fill_is_a_failure_not_an_exception(
    browser: Browser, tmp_path: Path
) -> None:
    """Typing the username fails (the field went away). The run returns a Failure with the step
    and what happened; nothing is raised to the caller."""
    s = await open_session(
        browser,
        tmp_path,
        Bank(),
        sensitive={"account_id": GOOD},
        wrap=lambda web: Flaky(web, nth("fill", 1), after=False),
    )
    try:
        result = await replay_in(s, BALANCE, {"account_id": GOOD})
    finally:
        await s.web.page.context.close()
    assert isinstance(result, Failure), result
    assert result.kind == "precondition_failed"  # it broke inside ensure_session
    assert result.observed is not None and "action_failed" in result.observed


async def test_submit_that_fires_then_errors_needs_a_human_and_opens_one_account(
    browser: Browser, tmp_path: Path
) -> None:
    """The click on "Open New Account" reaches the bank, then the surface reports an error.
    The step was marked committed before the click, so this is NeedsHuman, not a Failure a
    caller would retry, and the account is opened exactly once."""
    bank = Bank()
    approved = OPEN.model_copy(update={"status": "approved"})
    # Click 1 is Log In (ensure_session); click 2 is the irreversible submit.
    s = await open_session(
        browser,
        tmp_path,
        bank,
        sensitive={"funding_account_id": GOOD},
        wrap=lambda web: Flaky(web, nth("click", 2), after=True),
    )
    try:
        result = await replay_in(s, approved, OPEN_INPUTS, confirm=True)
    finally:
        await s.web.page.context.close()
    assert isinstance(result, NeedsHuman), result
    assert result.step_id == "s4"
    assert result.reason.startswith("action_failed after irreversible step s4 had run")
    assert bank.creates == 1


async def test_submit_that_errors_before_firing_still_needs_a_human(
    browser: Browser, tmp_path: Path
) -> None:
    """From outside, "failed before the click" and "failed after it" look the same. Committing
    before the action means both are treated as "it may have happened"."""
    bank = Bank()
    approved = OPEN.model_copy(update={"status": "approved"})
    s = await open_session(
        browser,
        tmp_path,
        bank,
        sensitive={"funding_account_id": GOOD},
        wrap=lambda web: Flaky(web, nth("click", 2), after=False),
    )
    try:
        result = await replay_in(s, approved, OPEN_INPUTS, confirm=True)
    finally:
        await s.web.page.context.close()
    assert isinstance(result, NeedsHuman), result
    assert bank.creates == 0  # it really did not happen; a person checks rather than guesses


# ---- funding account not found -----------------------------------------------------------------


async def run_open(browser: Browser, tmp_path: Path, bank: Bank, funding: str) -> RunResult:
    approved = OPEN.model_copy(update={"status": "approved"})
    s = await open_session(browser, tmp_path, bank, sensitive={"funding_account_id": funding})
    try:
        inputs = {**OPEN_INPUTS, "funding_account_id": funding}
        return await replay_in(s, approved, inputs, confirm=True)
    finally:
        await s.web.page.context.close()


async def test_unknown_funding_account_is_a_business_outcome(
    browser: Browser, tmp_path: Path
) -> None:
    """The funding list has loaded and does not offer the account. The run stops before the
    select, so the irreversible submit is never reached."""
    bank = Bank()
    result = await run_open(browser, tmp_path, bank, BAD)
    assert isinstance(result, BusinessOutcome), result
    assert (result.code, result.step_id) == ("funding_account_not_found", "s3")
    assert bank.creates == 0


async def test_funding_list_loading_late_is_waited_for(browser: Browser, tmp_path: Path) -> None:
    """An empty list is "not loaded yet", never "not found"."""
    bank = Bank(options_after_ms=1500)
    result = await run_open(browser, tmp_path, bank, GOOD)
    assert isinstance(result, Success), result
    assert bank.creates == 1
    late = Bank(options_after_ms=1500)
    result = await run_open(browser, tmp_path / "late", late, BAD)
    assert isinstance(result, BusinessOutcome) and result.code == "funding_account_not_found"
