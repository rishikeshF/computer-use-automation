"""Discovery end to end: a scripted fake model drives a real browser on a fake bank.

The fake model reads the same text a real one would and picks refs from it, so these tests check
the real loop: policy, approval, redaction, stop conditions and the recorded artifact. The bank is
served by Playwright route fulfilment, so nothing leaves the machine.
"""

import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

import pytest
from playwright.async_api import Browser, Route, async_playwright

from cua.core.action import Navigate
from cua.core.policy import Policy, PolicyConfig
from cua.core.result import Failure, NeedsHuman, Success
from cua.core.runlog import RunLog
from cua.discover.agent import Approver, DiscoveryRun, DiscoveryTask, discover
from cua.discover.recorder import record
from cua.llm.base import Reply, ToolCall, ToolResult, ToolSpec
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

ORIGIN = "https://bank.test"
BASE = f"{ORIGIN}/bank"
ACCOUNT = "13344"
PASSWORD = "hunter2-secret"
PAGES = {
    "/bank/index.htm": """
        <p>Username</p><input name="u">
        <p>Password</p><input type="password" name="p">
        <button onclick="location='/bank/overview.htm'">Log In</button>""",
    "/bank/overview.htm": f"""
        <h1>Accounts Overview</h1>
        <table><tr><th>Account</th><th>Balance</th></tr>
          <tr><td><a href="/bank/activity.htm?id={ACCOUNT}">{ACCOUNT}</a></td><td>$515.50</td></tr>
          <tr><td><a href="/bank/activity.htm?id=54321">54321</a></td><td>$10.00</td></tr>
        </table>
        <a href="#">Refresh</a>""",
    "/bank/activity.htm": f"""
        <h1>Account Details</h1>
        <table>
          <tr><td>Account Number:</td><td>{ACCOUNT}</td></tr>
          <tr><td>Balance:</td><td>$515.50</td></tr>
          <tr><td>Available:</td><td>$500.00</td></tr>
        </table>
        <button>Transfer</button>""",
}
POLICY = Policy(
    PolicyConfig(
        name="test",
        origins=[ORIGIN],
        allow_paths=["/bank/*"],
        actions=["click", "fill", "select", "navigate"],
        risk={
            "irreversible": [{"role": "button", "name": "transfer"}],
            "reversible": [{"role": "button", "name": "log in"}],
        },
        sensitive_inputs=["pass", "account"],
    )
)


async def serve(route: Route) -> None:
    path = route.request.url.removeprefix(ORIGIN).split("?")[0].split("#")[0]
    if path in PAGES:
        await route.fulfill(content_type="text/html", body=PAGES[path])
    else:
        await route.fulfill(status=404, body="not found")


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


@pytest.fixture
async def web(browser: Browser) -> AsyncIterator[WebSurface]:
    context = await browser.new_context()
    await context.route(f"{ORIGIN}/**", serve)
    surface = WebSurface(await context.new_page(), polite_delay_ms=0, action_timeout_ms=2_000)
    await surface.guard_navigation(POLICY.url_allowed)
    yield surface
    await context.close()


# ---- the fake model ---------------------------------------------------------------------------

Script = Callable[[str], tuple[str, dict[str, object]]]


class FakeLLM:
    """Plays a script. Each entry sees the latest page text and returns (tool, input)."""

    model = "fake"

    def __init__(self, script: Sequence[Script]):
        self.script = list(script)
        self.seen: list[str] = []  # everything the "model" was sent, to check for leaks
        self.results: list[ToolResult] = []

    def conversation(self, system: str, tools: Sequence[ToolSpec]) -> "FakeLLM":
        return self

    async def send(
        self,
        *,
        results: Sequence[ToolResult] = (),
        text: str = "",
        image_png: bytes | None = None,
    ) -> Reply:
        self.results += results
        self.seen += [text] + [r.text for r in results]
        if text:
            self.page = text
        name, args = self.script.pop(0)(self.page)
        return Reply(
            calls=[ToolCall(id=f"c{len(self.script)}", name=name, input=args)], stop="tool_use"
        )


def ref(page: str, pattern: str) -> str:
    """Ref of the first line of the rendered page matching `pattern`."""
    for line in page.splitlines():
        if re.search(pattern, line):
            return line.split("]")[0].lstrip("[")
    raise AssertionError(f"{pattern!r} not on page:\n{page}")


def step(tool: str, **args: object) -> Script:
    """A script entry. Any `ref=<regex>` is looked up on the page when the step runs."""

    def run(page: str) -> tuple[str, dict[str, object]]:
        resolved = {k: ref(page, str(v)) if k == "ref" else v for k, v in args.items()}
        return tool, resolved

    return run


async def never(_: str) -> bool:
    raise AssertionError("no approval expected")


async def run_task(
    web: WebSurface,
    tmp_path: Path,
    script: Sequence[Script],
    *,
    inputs: dict[str, str],
    start: str,
    outputs: Sequence[str] = (),
    approver: Approver = never,
    max_steps: int = 10,
) -> tuple[DiscoveryRun, FakeLLM, GuardedSurface, RunLog]:
    surface = GuardedSurface(web, POLICY, sensitive=POLICY.sensitive_values(inputs))
    log = RunLog(tmp_path, "run", surface.redact)
    await web.act(Navigate(url=start))
    llm = FakeLLM(script)
    task = DiscoveryTask(
        name="t", goal="g", inputs=inputs, outputs=list(outputs), base_url=BASE, max_steps=max_steps
    )
    run = await discover(task, surface, llm, log, approver=approver, send_screenshots=False)
    return run, llm, surface, log


# ---- tests ------------------------------------------------------------------------------------


async def test_balance_discovery_records_a_parameterized_artifact(
    web: WebSurface, tmp_path: Path
) -> None:
    script = [
        step("click", ref=r'link "\[REDACTED:account_id\]"', intent="Open the account"),
        step("extract", ref=r'row="Balance:"', output="balance", intent="Read the balance"),
        step("extract", ref=r'row="Available:"', output="available", intent="Read available"),
        step("done", summary="read both"),
    ]
    run, llm, _, log = await run_task(
        web,
        tmp_path,
        script,
        inputs={"account_id": ACCOUNT},
        start=f"{BASE}/overview.htm",
        outputs=["balance", "available"],
    )
    assert isinstance(run.result, Success)
    assert run.outputs == {"balance": "$515.50", "available": "$500.00"}

    # The model found the account by its redacted name; it never saw the number itself.
    assert not any(ACCOUNT in s for s in llm.seen)
    assert "*4321" in llm.seen[0]  # other account numbers are masked, not hidden
    assert ACCOUNT not in (log.dir / "log.jsonl").read_text(encoding="utf-8")

    cap = record(
        run.trace,
        name="get_account_balance",
        version="0.1.0",
        goal="g",
        description="d",
        inputs={"account_id": ACCOUNT},
        sensitive={"account_id"},
        base_url=BASE,
        vendor="test",
        model="fake",
        run_id="run",
    )
    body = cap.model_dump_json()
    assert ACCOUNT not in body
    assert cap.steps[0].post.url_pattern == "/bank/activity.htm?id={{account_id}}"
    assert cap.steps[0].post.landmark_text == "Account Details"
    assert [o.name for o in cap.outputs] == ["balance", "available"]
    assert cap.problems() == []


async def test_password_is_typed_without_the_model_seeing_it(
    web: WebSurface, tmp_path: Path
) -> None:
    script = [
        step("fill", ref=r'label="Username"', value="{{username}}", intent="Type the username"),
        step("fill", ref=r'label="Password"', value="{{password}}", intent="Type the password"),
        step("click", ref=r'button "Log In"', intent="Sign in"),
        step("done", summary="signed in"),
    ]
    run, llm, _, log = await run_task(
        web,
        tmp_path,
        script,
        inputs={"username": "alice", "password": PASSWORD},
        start=f"{BASE}/index.htm",
    )
    assert isinstance(run.result, Success)
    assert run.trace[2].url_after.endswith("/bank/overview.htm")
    assert not any(PASSWORD in s for s in llm.seen)
    assert PASSWORD not in (log.dir / "log.jsonl").read_text(encoding="utf-8")
    assert run.trace[1].value == "{{password}}"


async def test_irreversible_click_waits_for_a_human(web: WebSurface, tmp_path: Path) -> None:
    asked: list[str] = []

    async def decline(what: str) -> bool:
        asked.append(what)
        return False

    script = [
        step("click", ref=r'button "Transfer"', intent="Move money"),
        step("give_up", reason="transfer was declined"),
    ]
    run, llm, _, _ = await run_task(
        web,
        tmp_path,
        script,
        inputs={"account_id": ACCOUNT},
        start=f"{BASE}/activity.htm?id={ACCOUNT}",
        approver=decline,
    )
    assert asked == ["click button 'Transfer' on /bank/activity.htm"]
    assert llm.results[0].is_error and "human declined" in llm.results[0].text
    assert run.trace == []  # a declined action is not recorded
    assert isinstance(run.result, Failure) and run.result.kind == "gave_up"


async def test_actions_that_change_nothing_stop_the_run(web: WebSurface, tmp_path: Path) -> None:
    script = [step("click", ref=r'link "Refresh"', intent="Refresh")] * 5
    run, _, _, _ = await run_task(web, tmp_path, script, inputs={}, start=f"{BASE}/overview.htm")
    assert isinstance(run.result, Failure) and run.result.kind == "stuck"


async def test_bad_calls_are_reported_to_the_model_not_raised(
    web: WebSurface, tmp_path: Path
) -> None:
    script: list[Script] = [
        lambda _: ("click", {"ref": "e9999", "intent": "Click a ref that does not exist"}),
        lambda _: ("navigate", {"url": "https://evil.test/", "intent": "Leave the app"}),
        lambda _: ("fill", {"ref": "e1"}),  # missing fields
        step("done", summary="ok"),
        step("request_human", reason="unsure"),
    ]
    run, llm, _, _ = await run_task(
        web, tmp_path, script, inputs={}, start=f"{BASE}/overview.htm", outputs=["balance"]
    )
    errors = [r.text for r in llm.results]
    assert "not in the latest observation" in errors[0]
    assert "blocked by policy" in errors[1]
    assert "invalid tool call" in errors[2]
    assert "extract ['balance'] first" in errors[3]  # done refused until outputs exist
    assert isinstance(run.result, NeedsHuman)
    assert all(r.is_error for r in llm.results)


async def test_step_limit(web: WebSurface, tmp_path: Path) -> None:
    script = [
        step("click", ref=r'link "\[REDACTED:account_id\]"', intent="Open"),
        step("navigate", url="{{base_url}}/overview.htm", intent="Back"),
    ] * 3
    run, _, _, log = await run_task(
        web,
        tmp_path,
        script,
        inputs={"account_id": ACCOUNT},
        start=f"{BASE}/overview.htm",
        max_steps=3,
    )
    assert isinstance(run.result, Failure) and run.result.kind == "step_limit"
    result = json.loads((log.dir / "result.json").read_text(encoding="utf-8"))
    assert result["kind"] == "step_limit"
