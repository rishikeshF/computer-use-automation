"""A fake ParaBank for replay and handoff tests. No model, no network.

It copies the real one where it matters:
  - no session: a page shows the login form AND "An internal error has occurred"
  - a bad login lands on login.htm under an "Error!" heading
  - the account rows arrive after the table frame (filled by script, like the real XHR)
  - the session is a cookie, so clearing cookies expires it
  - opening an account is one POST behind a button; `Bank.creates` counts them, which is how
    a test proves an irreversible step ran exactly once
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, Route

from cua.core.action import Action
from cua.core.artifact import (
    AppInfo,
    Capability,
    Checkpoint,
    Condition,
    InputSpec,
    MissingOption,
    OutcomeDetector,
    OutputSpec,
    ParamValue,
    Provenance,
    Retry,
    Step,
)
from cua.core.faults import FaultPlan
from cua.core.locator import LabelText, LocatorChain, RoleName, TableCell, TextAnchor
from cua.core.policy import Policy, PolicyConfig
from cua.core.result import RunResult
from cua.core.runlog import RunLog
from cua.replay.engine import Handoff, ReplayOptions, replay
from cua.surface.base import Observation, Resolved, SurfaceError, Unresolved
from cua.surface.faults import WebFaults
from cua.surface.guarded import ControlGate, GuardedSurface
from cua.surface.variant import install_variant
from cua.surface.web import WebSurface

ORIGIN = "https://bank.test"
BASE = f"{ORIGIN}/bank"
GOOD, BAD = "13344", "99999"
NEW_ID = "77777"
CREDS = {"username": "alice", "password": "hunter2-secret"}
FAILED_LOGIN = "The username and password could not be verified."


@dataclass
class Bank:
    rows_after_ms: int = 0  # -1: the rows never arrive
    balance_header: str = "Balance*"
    expire_on_create: bool = False  # the session dies right after an account is opened
    options_after_ms: int = 0  # the funding list fills in late, like ParaBank's XHR
    creates: int = 0  # accounts opened: the irreversible action, counted server side


LOGIN = """<h2>Customer Login</h2>
  <p>Username</p><input name="u"><p>Password</p><input type="password" name="p">
  <button onclick="if (document.querySelector('[name=u]').value === 'alice') {
      document.cookie='session=1; path=/'; location='/bank/overview.htm';
    } else { location='/bank/login.htm'; }">Log In</button>"""
ERROR = "<h1>Error!</h1><p>An internal error has occurred and has been logged.</p>"
SIDEBAR = """<h2>Account Services</h2>
  <a href="/bank/open.htm">Open New Account</a> <a href="/bank/overview.htm">Accounts Overview</a>
  <a href="#">Log Out</a>"""


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
    return f"""{SIDEBAR}
      <h1>Accounts Overview</h1>
      <table><thead><tr>
        <th>Account</th><th>{bank.balance_header}</th><th>Available Amount</th>
      </tr></thead>
      <tbody id="rows"></tbody></table>{fill}"""


def open_account(bank: Bank) -> str:
    expire = (
        "document.cookie = 'session=; Max-Age=0; path=/'; location.reload();"
        if bank.expire_on_create
        else ""
    )
    return f"""{SIDEBAR}
      <h1>Open New Account</h1>
      <p>What type of Account would you like to open?</p>
      <select id="type"><option>CHECKING</option><option>SAVINGS</option></select>
      <p>Choose the account that funds it.</p>
      <select id="from"></select>
      <div><input type="button" value="Open New Account" onclick="openIt()"></div>
      <div id="result" style="display:none">
        <h1>Account Opened!</h1>
        <p>Your new account number: <a id="newId" href="#">?</a></p>
      </div>
      <script>
        setTimeout(() => {{
          document.getElementById('from').innerHTML =
            '<option>{GOOD}</option><option>54321</option>';
        }}, {bank.options_after_ms});
        async function openIt() {{
          const id = await (await fetch('/bank/create', {{method: 'POST'}})).text();
          document.getElementById('newId').textContent = id;
          document.getElementById('result').style.display = 'block';
          {expire}
        }}
      </script>"""


def handler(bank: Bank):  # type: ignore[no-untyped-def]
    async def serve(route: Route) -> None:
        path = route.request.url.removeprefix(ORIGIN).split("?")[0]
        signed_in = "session=1" in (await route.request.all_headers()).get("cookie", "")
        if path == "/bank/index.htm":
            body = LOGIN
        elif path == "/bank/login.htm":
            body = LOGIN + f"<h1>Error!</h1><p>{FAILED_LOGIN}</p>"
        elif not signed_in and path in ("/bank/overview.htm", "/bank/open.htm", "/bank/create"):
            body = LOGIN + ERROR
        elif path == "/bank/overview.htm":
            body = overview(bank)
        elif path == "/bank/open.htm":
            body = open_account(bank)
        elif path == "/bank/create" and route.request.method == "POST":
            bank.creates += 1
            await route.fulfill(content_type="text/plain", body=NEW_ID)
            return
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
        risk={
            "reversible": [{"role": "button", "name": "log in"}],
            "irreversible": [{"role": "button", "name": "open new account"}],
        },
        sensitive_inputs=["pass", "user", "account_id$"],
    )
)
PROV = Provenance(discovered_at="2026-10-16T00:00:00+00:00", model="test", run_id="r", goal="g")
OVERVIEW_CP = Checkpoint(url_pattern="/bank/overview.htm", landmark_text="Accounts Overview")
FAST = 3_000  # step timeout in these tests


def cell(col: str) -> LocatorChain:
    return LocatorChain(strategies=(TableCell(role="cell", row="{{account_id}}", col=col),))


def chain(*strategies: RoleName | LabelText | TableCell | TextAnchor) -> LocatorChain:
    return LocatorChain(strategies=strategies)


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
                    chain(RoleName(role="columnheader", name="Account")),
                    chain(RoleName(role="columnheader", name="Balance*")),
                    chain(TableCell(role="cell", row="*", col="Balance*")),
                ),
                absent=(chain(RoleName(role="link", name="{{account_id}}")),),
            ),
        ),
    ),
    risk="read_only",
    provenance=PROV,
).sealed()


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
            target=chain(LabelText(role="textbox", label="Username")),
            value=ParamValue(param="username"),
            risk="reversible",
            timeout_ms=FAST,
        ),
        Step(
            id="s3",
            intent="Type password",
            action="fill",
            target=chain(LabelText(role="textbox", label="Password")),
            value=ParamValue(param="password"),
            risk="reversible",
            timeout_ms=FAST,
        ),
        Step(
            id="s4",
            intent="Sign in",
            action="click",
            target=chain(RoleName(role="button", name="Log In")),
            post=OVERVIEW_CP,
            risk="reversible",
            timeout_ms=FAST,
        ),
    ),
    success=OVERVIEW_CP,
    outcomes=(
        OutcomeDetector(
            code="invalid_credentials",
            message="The bank did not accept the username and password.",
            check_after=("s4",),
            when=Condition(
                page=Checkpoint(url_pattern="/bank/login.htm", landmark_text="Error!"),
                present=(chain(RoleName(role="text", name=FAILED_LOGIN)),),
            ),
        ),
    ),
    risk="reversible",
    provenance=PROV,
).sealed()


FUNDING_LIST = LocatorChain(
    strategies=(LabelText(role="combobox", label="Choose the account that funds it."),)
)
OPEN_CP = Checkpoint(url_pattern="/bank/open.htm", landmark_text="Open New Account")
OPENED_CP = Checkpoint(url_pattern="/bank/open.htm", landmark_text="Account Opened!")
OPEN_STEPS = (
    Step(
        id="s1",
        intent="Open the Open New Account page",
        action="navigate",
        value="{{base_url}}/open.htm",
        post=OPEN_CP,
        retry=Retry(max=2, backoff_ms=50),
        timeout_ms=FAST,
    ),
    Step(
        id="s2",
        intent="Choose the account type",
        action="select",
        target=chain(
            LabelText(role="combobox", label="What type of Account would you like to open?")
        ),
        value=ParamValue(param="account_type"),
        pre=OPEN_CP,
        risk="reversible",
        timeout_ms=FAST,
    ),
    Step(
        id="s3",
        intent="Choose the funding account",
        action="select",
        target=FUNDING_LIST,
        value=ParamValue(param="funding_account_id"),
        pre=OPEN_CP,
        risk="reversible",
        timeout_ms=FAST,
    ),
    Step(
        id="s4",
        intent="Open the account",
        action="click",
        target=chain(RoleName(role="button", name="Open New Account")),
        pre=OPEN_CP,
        post=OPENED_CP,
        risk="irreversible",
        timeout_ms=FAST,
    ),
    Step(
        id="s5",
        intent="Read the new account number",
        action="extract",
        target=chain(
            TextAnchor(role="link", anchor="Your new account number:", direction="right_of")
        ),
        value="new_account_id",
        pre=OPENED_CP,
        timeout_ms=FAST,
    ),
)
OPEN = Capability(
    id="open_new_account@0.1.0",
    name="open_new_account",
    version="0.1.0",
    description="Open an account",
    app=AppInfo(vendor="test"),
    inputs=(InputSpec(name="account_type"), InputSpec(name="funding_account_id", sensitive=True)),
    outputs=(OutputSpec(name="new_account_id", source_step="s5"),),
    preconditions=("ensure_session",),
    steps=OPEN_STEPS,
    outcomes=(
        OutcomeDetector(
            code="funding_account_not_found",
            message="The funding account is not one of this customer's accounts.",
            check_before=("s3",),
            when=Condition(
                page=OPEN_CP,
                missing_options=(
                    MissingOption(
                        control=FUNDING_LIST,
                        option="{{funding_account_id}}",
                    ),
                ),
            ),
        ),
    ),
    success=OPENED_CP,
    risk="irreversible",
    provenance=PROV,
).sealed()
OPEN_INPUTS = {"account_type": "SAVINGS", "funding_account_id": GOOD}

# Opens the account, then goes back to the overview: a page load after the commit.
OPEN_THEN_OVERVIEW = OPEN.model_copy(
    update={
        "steps": OPEN_STEPS
        + (
            Step(
                id="s6",
                intent="Back to the overview",
                action="navigate",
                value="{{base_url}}/overview.htm",
                post=OVERVIEW_CP,
                retry=Retry(max=2, backoff_ms=50),
                timeout_ms=FAST,
            ),
        ),
        "success": OVERVIEW_CP,
    }
).sealed()


class Flaky:
    """A surface that breaks on one chosen action, the way a real browser can.

    after=True runs the action first, then raises: the click landed and the page never answered
    (connection reset, a navigation that hung). after=False raises without running it."""

    def __init__(self, inner: WebSurface, breaks: Callable[[Action], bool], *, after: bool):
        self._inner = inner
        self._breaks = breaks
        self._after = after
        self.broke = False

    @property
    def url(self) -> str:
        return self._inner.url

    async def observe(
        self, *, screenshot: bool = True, mask_values: Sequence[str] = ()
    ) -> Observation:
        return await self._inner.observe(screenshot=screenshot, mask_values=mask_values)

    async def resolve(self, chain: LocatorChain, *, timeout_ms: int = 0) -> Resolved | Unresolved:
        return await self._inner.resolve(chain, timeout_ms=timeout_ms)

    async def act(self, action: Action) -> None:
        if self.broke or not self._breaks(action):
            await self._inner.act(action)
            return
        self.broke = True
        if self._after:
            await self._inner.act(action)
        raise SurfaceError("not_actionable", "connection reset while the page was answering")


@dataclass
class Session:
    """One test's browser, surface and log. `web` is the raw surface (the "human" uses it)."""

    web: WebSurface
    surface: GuardedSurface
    log: RunLog
    faults: WebFaults | None


async def open_session(
    browser: Browser,
    tmp_path: Path,
    bank: Bank,
    *,
    sensitive: dict[str, str],
    faults: FaultPlan | None = None,
    gate: ControlGate | None = None,
    wrap: Callable[[WebSurface], Flaky] | None = None,
    policy: Policy = POLICY,
    variant: dict[str, str] | None = None,
) -> Session:
    context = await browser.new_context()
    if variant:
        await install_variant(context, variant)  # another tenant's words, as in the CLI
    await context.route(f"{ORIGIN}/**", handler(bank))
    web = WebSurface(await context.new_page(), polite_delay_ms=0, action_timeout_ms=2_000)
    await web.guard_navigation(POLICY.url_allowed)
    inner = wrap(web) if wrap else web
    surface = GuardedSurface(inner, policy, sensitive={**sensitive, **CREDS}, gate=gate)
    log = RunLog(tmp_path, "run", surface.redact, base=tmp_path)
    injector = None
    if faults is not None:
        injector = WebFaults(web, faults)
        await injector.install("/bank/overview.htm")
    return Session(web, surface, log, injector)


async def replay_in(
    s: Session,
    cap: Capability,
    inputs: dict[str, str],
    *,
    confirm: bool = False,
    handoff: Handoff | None = None,
    creds: dict[str, str] = CREDS,
) -> RunResult:
    async def sign_in() -> RunResult:
        opts = ReplayOptions(base_url=BASE, handoff=handoff)
        return await replay(SESSION, creds, s.surface, s.log, opts)

    opts = ReplayOptions(
        base_url=BASE,
        confirm=confirm,
        preconditions={"ensure_session": sign_in},
        hooks=s.faults,
        handoff=handoff,
    )
    return await replay(cap, inputs, s.surface, s.log, opts)
