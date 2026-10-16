"""Human handoff end to end on the fake bank: a draft open_new_account pauses at its irreversible
step, and a scripted operator answers through control.json, as `cua operator` would.

`Bank.creates` counts accounts opened on the server, so each test proves how many times the
irreversible action really ran, and by whom.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.async_api import Browser, async_playwright

from cua.core.result import Failure, NeedsHuman, RunResult, Success
from cua.handoff.control import (
    ControlFile,
    Intervention,
    Lease,
    approve,
    decline,
    release,
    take,
)
from cua.handoff.runner import HumanHandoff
from cua.surface.capture import HumanCapture
from cua.surface.web import WebSurface
from tests.fakebank import GOOD, NEW_ID, OPEN, OPEN_INPUTS, POLICY, Bank, open_session, replay_in

Operator = Callable[[ControlFile, WebSurface], Awaitable[None]]


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


@dataclass
class Outcome:
    result: RunResult
    bank: Bank
    control: ControlFile
    capture: HumanCapture
    log: list[dict[str, object]]


async def handoff_run(
    browser: Browser,
    tmp_path: Path,
    operator: Operator | None,
    *,
    taken_before_start: bool = False,
    timeout_s: float = 15.0,
) -> Outcome:
    bank = Bank()
    ctl = ControlFile.create(tmp_path / "run", "run")
    lease = Lease(ctl)
    if taken_before_start:
        ctl.update(take("tester"))
    s = await open_session(
        browser, tmp_path, bank, sensitive={"funding_account_id": GOOD}, gate=lease
    )
    capture = HumanCapture(s.web, POLICY, s.surface.redact, s.log, human_holds=ctl.human_holds)
    await capture.install()
    desk = HumanHandoff(
        ctl,
        s.log,
        window=capture,
        notify=lambda _: None,
        timeout_s=timeout_s,
        poll_s=0.05,
        lease=lease,
    )
    task = asyncio.create_task(operator(ctl, s.web)) if operator else None
    try:
        result = await replay_in(s, OPEN, OPEN_INPUTS, handoff=desk)
        if task is not None:
            await asyncio.wait_for(task, 5)
    finally:
        await s.web.page.context.close()
    lines = (s.log.dir / "log.jsonl").read_text(encoding="utf-8").splitlines()
    return Outcome(result, bank, ctl, capture, [json.loads(line) for line in lines])


async def waiting(ctl: ControlFile, kind: str) -> Intervention:
    """What an operator sees in `cua operator list`, once the run asks."""
    for _ in range(400):
        iv = ctl.read().intervention
        if iv is not None and iv.kind == kind:
            return iv
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {kind} intervention appeared")


# ---- approve: automation does the step -------------------------------------------------------


async def test_operator_approves_the_irreversible_step(browser: Browser, tmp_path: Path) -> None:
    async def operator(ctl: ControlFile, _: WebSurface) -> None:
        iv = await waiting(ctl, "approval")
        # What the person is shown: capability, step, reason, URL and a masked screenshot.
        assert (iv.capability, iv.step_id, iv.intent) == (OPEN.id, "s4", "Open the account")
        assert "irreversible" in iv.reason and iv.url.endswith("/bank/open.htm")
        assert iv.screenshot and (ctl.dir / iv.screenshot).exists()
        assert GOOD not in iv.model_dump_json()  # the funding account is redacted
        ctl.update(approve("tester", iv.id))

    out = await handoff_run(browser, tmp_path, operator)
    assert isinstance(out.result, Success), out.result
    assert out.result.outputs == {"new_account_id": NEW_ID}
    assert out.bank.creates == 1
    assert out.result.evidence.interventions == ("s4: approved by tester",)
    events = [e["event"] for e in out.log]
    assert events.index("intervention") < events.index("intervention_answered")
    assert events.index("intervention_answered") < events.index("committed")
    assert out.control.read().owner == "automation" and out.control.read().intervention is None


async def test_operator_declines(browser: Browser, tmp_path: Path) -> None:
    async def operator(ctl: ControlFile, _: WebSurface) -> None:
        iv = await waiting(ctl, "approval")
        ctl.update(decline("tester", iv.id))

    out = await handoff_run(browser, tmp_path, operator)
    assert isinstance(out.result, Failure) and out.result.kind == "policy_violation", out.result
    assert out.result.observed == "declined by tester"
    assert out.bank.creates == 0


async def test_nobody_answers_in_time(browser: Browser, tmp_path: Path) -> None:
    out = await handoff_run(browser, tmp_path, None, timeout_s=0.5)
    assert isinstance(out.result, NeedsHuman), out.result
    assert out.result.intervention_id == "s4-1" and "nobody answered" in out.result.reason
    assert out.bank.creates == 0


# ---- take, act, release: the person does the step -----------------------------------------------


async def test_person_takes_control_and_opens_the_account(browser: Browser, tmp_path: Path) -> None:
    async def operator(ctl: ControlFile, web: WebSurface) -> None:
        await waiting(ctl, "approval")
        ctl.update(take("tester"))
        await asyncio.sleep(0.2)  # the run notices and brings the window to the front
        # The person clicks in the live window. Automation is paused; the capture logs it.
        await web.page.click("input[value='Open New Account']")
        await web.page.wait_for_selector("text=Account Opened!")
        ctl.update(release("tester"))

    out = await handoff_run(browser, tmp_path, operator)
    assert isinstance(out.result, Success), out.result
    assert out.result.outputs == {"new_account_id": NEW_ID}
    assert out.bank.creates == 1  # the person's click; automation did not click as well
    assert out.result.evidence.interventions == ("s4: done by tester in the browser",)
    (human,) = [e for e in out.log if e["event"] == "human_action"]
    assert (human["kind"], human["name"], human["risk"]) == (
        "click",
        "Open New Account",
        "irreversible",
    )
    events = [e["event"] for e in out.log]
    assert "control_taken" in events and "control_released" in events


async def test_release_without_doing_the_step_needs_a_human(
    browser: Browser, tmp_path: Path
) -> None:
    async def operator(ctl: ControlFile, _: WebSurface) -> None:
        await waiting(ctl, "approval")
        ctl.update(take("tester"))
        ctl.update(release("tester"))

    out = await handoff_run(browser, tmp_path, operator)
    assert isinstance(out.result, NeedsHuman), out.result
    assert "without approving or doing s4" in out.result.reason
    assert out.bank.creates == 0


async def test_automation_actions_are_not_captured_as_human(
    browser: Browser, tmp_path: Path
) -> None:
    """Playwright's clicks fire the same DOM events as a person's. Only the control file can
    tell them apart, and while automation holds control nothing is logged as human."""

    async def operator(ctl: ControlFile, _: WebSurface) -> None:
        iv = await waiting(ctl, "approval")
        ctl.update(approve("tester", iv.id))

    out = await handoff_run(browser, tmp_path, operator)
    assert isinstance(out.result, Success), out.result
    assert out.capture.actions == ()


# ---- a person takes control unasked -------------------------------------------------------------


async def test_unasked_takeover_pauses_then_the_step_runs_again(
    browser: Browser, tmp_path: Path
) -> None:
    """Control is taken before the run's first action. The surface refuses to act, the run waits
    for the release, runs that step again from its checkpoint, and later still asks before the
    irreversible one."""

    async def operator(ctl: ControlFile, _: WebSurface) -> None:
        iv = await waiting(ctl, "takeover")
        assert iv.step_id == "s1"
        ctl.update(release("tester"))
        iv = await waiting(ctl, "approval")
        ctl.update(approve("tester", iv.id))

    out = await handoff_run(browser, tmp_path, operator, taken_before_start=True)
    assert isinstance(out.result, Success), out.result
    assert out.bank.creates == 1
    assert out.result.evidence.interventions == (
        "ensure_session: s1: tester held control; step run again",
        "s4: approved by tester",
    )
