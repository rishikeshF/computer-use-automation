"""Replay end to end on a fake ParaBank, with faults. No model, no network.

The fake bank copies the real one where it matters:
  - no session: the overview shows the login form AND "An internal error has occurred"
  - the account rows arrive after the table frame (filled by script, like the real XHR)
  - the session is a cookie, so clearing cookies expires it
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.async_api import Browser, Route, async_playwright

from cua.core.artifact import (
    AppInfo,
    Capability,
    Checkpoint,
    Condition,
    InputSpec,
    OutcomeDetector,
    OutputSpec,
    ParamValue,
    Provenance,
    Retry,
    Step,
)
from cua.core.faults import FaultPlan
from cua.core.locator import LabelText, LocatorChain, RoleName, TableCell
from cua.core.policy import Policy, PolicyConfig
from cua.core.result import BusinessOutcome, Failure, RunResult, Success
from cua.core.runlog import RunLog
from cua.replay.engine import ReplayOptions, replay
from cua.surface.faults import WebFaults
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

ORIGIN = "https://bank.test"
BASE = f"{ORIGIN}/bank"
GOOD, BAD = "13344", "99999"
CREDS = {"username": "alice", "password": "hunter2-secret"}


@dataclass
class Bank:
    rows_after_ms: int = 0  # -1: the rows never arrive
    balance_header: str = "Balance*"


LOGIN = """<h2>Customer Login</h2>
  <p>Username</p><input name="u"><p>Password</p><input type="password" name="p">
  <button onclick="document.cookie='session=1; path=/'; location='/bank/overview.htm'">
    Log In</button>"""


def overview(bank: Bank) -> str:
    rows = f"""
      <tr><td><a href="/bank/activity.htm?id={GOOD}">{GOOD}</a></td>
          <td>$515.50</td><td>$500.00</td></tr>
      <tr><td><a href="/bank/activity.htm?id=54321">54321</a></td>
          <td>$10.00</td><td>$10.00</td></tr>
      <tr><td>Total</td><td>$525.50</td><td></td></tr>"""
    # The rows arrive by script after a delay, like ParaBank's XHR-filled table.
    script = f"document.getElementById('rows').innerHTML = `{rows}`"
    fill = (
        ""
        if bank.rows_after_ms < 0
        else f"<script>setTimeout(() => {{ {script} }}, {bank.rows_after_ms})</script>"
    )
    return f"""<h2>Account Services</h2><a href="/bank/overview.htm">Accounts Overview</a>
      <a href="#">Log Out</a>
      <h1>Accounts Overview</h1>
      <table><thead><tr>
        <th>Account</th><th>{bank.balance_header}</th><th>Available Amount</th>
      </tr></thead>
      <tbody id="rows"></tbody></table>{fill}"""


def handler(bank: Bank):  # type: ignore[no-untyped-def]
    async def serve(route: Route) -> None:
        path = route.request.url.removeprefix(ORIGIN).split("?")[0]
        cookie = (await route.request.all_headers()).get("cookie", "")
        if path == "/bank/index.htm":
            body = LOGIN
        elif path == "/bank/overview.htm":
            body = (
                overview(bank)
                if "session=1" in cookie
                else (
                    LOGIN
                    + "<h1>Error!</h1><p>An internal error has occurred and has been logged.</p>"
                )
            )
        else:
            await route.fulfill(status=404, body="not found")
            return
        await route.fulfill(content_type="text/html", body=body)

    return serve


POLICY = Policy(
    PolicyConfig(
        name="test",
        origins=[ORIGIN],
        allow_paths=["/bank/*"],
        actions=["click", "fill", "select", "navigate"],
        risk={"reversible": [{"role": "button", "name": "log in"}]},
        sensitive_inputs=["pass", "user", "account"],
    )
)
PROV = Provenance(discovered_at="2026-10-16T00:00:00+00:00", model="test", run_id="r", goal="g")
OVERVIEW_CP = Checkpoint(url_pattern="/bank/overview.htm", landmark_text="Accounts Overview")
FAST = 3_000  # step timeout in these tests


def cell(col: str) -> LocatorChain:
    return LocatorChain(strategies=(TableCell(role="cell", row="{{account_id}}", col=col),))


BALANCE = Capability(
    id="get_account_balance@0.1.0",
    name="get_account_balance",
    version="0.1.0",
    description="Balance of one account",
    app=AppInfo(vendor="test"),
    inputs=(InputSpec(name="account_id", sensitive=True),),
    outputs=(
        OutputSpec(name="balance", type="decimal", unit="USD", source_step="s2"),
        OutputSpec(name="available", type="decimal", unit="USD", source_step="s3"),
    ),
    preconditions=("ensure_session",),
    steps=(
        Step(
            id="s1",
            intent="Open the Accounts Overview page",
            action="navigate",
            value="{{base_url}}/overview.htm",
            post=OVERVIEW_CP,
            retry=Retry(max=2, backoff_ms=50),
            timeout_ms=FAST,
        ),
        Step(
            id="s2",
            intent="Read balance",
            action="extract",
            target=cell("Balance*"),
            value="balance",
            pre=OVERVIEW_CP,
            timeout_ms=FAST,
        ),
        Step(
            id="s3",
            intent="Read available",
            action="extract",
            target=cell("Available Amount"),
            value="available",
            pre=OVERVIEW_CP,
            timeout_ms=FAST,
        ),
    ),
    success=OVERVIEW_CP,
    outcomes=(
        OutcomeDetector(
            code="account_not_found",
            message="No account with the given account_id on Accounts Overview.",
            check_before=("s2", "s3"),
            when=Condition(
                page=OVERVIEW_CP,
                present=(
                    LocatorChain(strategies=(RoleName(role="columnheader", name="Account"),)),
                    LocatorChain(strategies=(RoleName(role="columnheader", name="Balance*"),)),
                    LocatorChain(strategies=(TableCell(role="cell", row="*", col="Balance*"),)),
                ),
                absent=(LocatorChain(strategies=(RoleName(role="link", name="{{account_id}}"),)),),
            ),
        ),
    ),
    risk="read_only",
    provenance=PROV,
).sealed()


def labelled(label: str) -> LocatorChain:
    return LocatorChain(strategies=(LabelText(role="textbox", label=label),))


SESSION = Capability(
    id="ensure_session@0.1.0",
    name="ensure_session",
    version="0.1.0",
    description="Sign in",
    app=AppInfo(vendor="test"),
    inputs=(InputSpec(name="username", sensitive=True), InputSpec(name="password", sensitive=True)),
    steps=(
        Step(
            id="s1",
            intent="Open login",
            action="navigate",
            value="{{base_url}}/index.htm",
            post=Checkpoint(url_pattern="/bank/index.htm"),
            timeout_ms=FAST,
        ),
        Step(
            id="s2",
            intent="Type user",
            action="fill",
            target=labelled("Username"),
            value=ParamValue(param="username"),
            risk="reversible",
            timeout_ms=FAST,
        ),
        Step(
            id="s3",
            intent="Type password",
            action="fill",
            target=labelled("Password"),
            value=ParamValue(param="password"),
            risk="reversible",
            timeout_ms=FAST,
        ),
        Step(
            id="s4",
            intent="Sign in",
            action="click",
            target=LocatorChain(strategies=(RoleName(role="button", name="Log In"),)),
            post=OVERVIEW_CP,
            risk="reversible",
            timeout_ms=FAST,
        ),
    ),
    success=OVERVIEW_CP,
    risk="reversible",
    provenance=PROV,
).sealed()


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
) -> tuple[RunResult, RunLog, WebFaults | None]:
    context = await browser.new_context()
    await context.route(f"{ORIGIN}/**", handler(bank or Bank()))
    web = WebSurface(await context.new_page(), polite_delay_ms=0, action_timeout_ms=2_000)
    await web.guard_navigation(POLICY.url_allowed)
    surface = GuardedSurface(web, POLICY, sensitive={"account_id": account, **CREDS})
    log = RunLog(tmp_path, "run", surface.redact, base=tmp_path)
    injector = None
    if faults is not None:
        injector = WebFaults(web, faults)
        await injector.install("/bank/overview.htm")

    async def sign_in() -> RunResult:
        return await replay(SESSION, CREDS, surface, log, ReplayOptions(base_url=BASE))

    opts = ReplayOptions(base_url=BASE, preconditions={"ensure_session": sign_in}, hooks=injector)
    try:
        result = await replay(cap, {"account_id": account}, surface, log, opts)
    finally:
        await context.close()
    return result, log, injector


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
