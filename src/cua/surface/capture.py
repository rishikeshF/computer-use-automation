"""Capture: what a person does in the live window while they hold control of a run.

Listeners injected into every page report clicks, typing and selections to Python. Each one is
described the way the rest of the system describes controls (role and name), run through the
policy for its risk, redacted and logged. Typed text never leaves the page: a fill is logged as
"[REDACTED]", always.

Playwright's own clicks fire the same DOM events, so the listeners cannot tell a person from
automation. The control file can: an event counts only while a human holds control.

Policy and people: a person's click cannot be stopped from Python before it lands (the event
arrives after the fact). So each one is checked as it happens and the verdict is logged, and the
network guard (WebSurface.guard_navigation) still aborts any page load the policy denies, for
example the admin page, whoever clicked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict

from cua.core.action import Action, Click, Fill, Select
from cua.core.policy import Policy, Risk
from cua.core.redact import Redactor
from cua.core.runlog import RunLog, utc_now
from cua.core.url import canonical_path
from cua.surface.web import WebSurface

_BINDING = "__cuaHuman"

# Installed in every document. Reports role, name and label, never a typed value.
_LISTENERS_JS = (Path(__file__).parent / "capture.js").read_text(encoding="utf-8")


@dataclass(frozen=True)
class _Target:
    """A control as the policy needs it (role, name, label)."""

    role: str
    name: str
    label: str | None


class HumanAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["click", "fill", "select"]
    role: str
    name: str
    label: str | None = None
    value: str | None = None  # select: the option (redacted). fill: always "[REDACTED]"
    path: str  # canonical path of the page it happened on
    risk: Risk
    verdict: str  # what policy.check() says about it (allow / deny)
    rule: str
    at: str


class HumanCapture:
    def __init__(
        self,
        web: WebSurface,
        policy: Policy,
        redact: Redactor,
        log: RunLog,
        *,
        human_holds: Callable[[], bool],
    ):
        self._web = web
        self._policy = policy
        self._redact = redact
        self._log = log
        self._human_holds = human_holds
        self._actions: list[HumanAction] = []

    @property
    def actions(self) -> Sequence[HumanAction]:
        return tuple(self._actions)

    def irreversible(self, since: int) -> bool:
        return any(a.risk == "irreversible" for a in self._actions[since:])

    async def install(self) -> None:
        """Listen in every page and frame, now and after each navigation."""
        context = self._web.page.context
        await context.expose_binding(_BINDING, self._on_event)
        await context.add_init_script(_LISTENERS_JS)
        for frame in self._web.page.frames:
            with suppress(PlaywrightError):  # a frame mid-navigation gets the init script
                await frame.evaluate(_LISTENERS_JS)

    async def focus(self) -> None:
        await self._web.page.bring_to_front()

    async def _on_event(self, source: dict[str, Any], event: dict[str, Any]) -> None:
        if not self._human_holds():
            return  # automation's own clicks fire the same events
        kind = event.get("kind")
        target = _Target(
            role=str(event.get("role") or ""),
            name=str(event.get("name") or ""),
            label=str(event["label"]) if event.get("label") else None,
        )
        action: Action
        value: str | None = None
        if kind == "click":
            action = Click(ref="human")
        elif kind == "fill":
            action = Fill(ref="human", value="", sensitive=True)
            value = "[REDACTED]"
        elif kind == "select":
            option = str(event.get("option") or "")
            action = Select(ref="human", option=option)
            value = self._redact(option)
        else:
            return
        url = source["frame"].url if "frame" in source else self._web.url
        # The person at the controls is the approver, so irreversible is not "needs_approval"
        # here. The verdict still says when policy would deny the action outright.
        decision = self._policy.check(action, url=url, target=target, irreversible_approved=True)
        found = HumanAction(
            kind=kind,
            role=target.role,
            name=self._redact(target.name),
            label=self._redact(target.label) if target.label else None,
            value=value,
            path=canonical_path(url),
            risk=decision.risk,
            verdict=decision.verdict,
            rule=decision.rule,
            at=utc_now(),
        )
        self._actions.append(found)
        self._log.event("human_action", **found.model_dump())
