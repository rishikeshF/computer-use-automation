"""Fault injection in our own browser: the Playwright side of a FaultPlan.

Everything happens client-side. Route interception answers or delays requests before they leave
the browser, and session expiry just forgets our cookies. The app never sees extra traffic, so
this is safe to run against a shared public demo.

The replay engine calls before_step() before each step of the capability it was asked to run
(not before steps of preconditions like ensure_session), so a fault always hits the flow under
test at a predictable point.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Route

from cua.core.artifact import Step
from cua.core.faults import FaultPlan
from cua.core.url import canonical_path
from cua.surface.web import WebSurface

# What ParaBank itself shows on a server fault, so the injected page looks like the real thing.
_ERROR_PAGE = """<html><head><title>ParaBank | Error</title></head><body>
<h1 class="title">Error!</h1>
<p class="error">An internal error has occurred and has been logged.</p>
</body></html>"""


class WebFaults:
    def __init__(self, web: WebSurface, plan: FaultPlan):
        self._web = web
        self._plan = plan
        self._http500_left = plan.http500_times
        self._http500_path = plan.http500_path
        self._expired = False
        # Route faults wait for the first step of the capability under test, so signing in
        # (ensure_session also loads the overview) never absorbs a fault meant for the flow.
        self._armed = False
        self._dialog_sent = False
        self.injected: list[str] = []  # what actually happened, for evidence

    async def install(self, start_path: str | None) -> None:
        """Install the route-level faults. `start_path` is the page step s1 opens; http500
        targets it unless the plan names another path."""
        self._http500_path = self._http500_path or start_path
        if self._plan.slow_ms or self._plan.http500_times:
            # Registered last, so it runs before the network guard; a fault only ever answers
            # or delays, it never lets through something the guard would stop.
            await self._web.page.context.route("**/*", self._route)

    async def _route(self, route: Route) -> None:
        request = route.request
        if not self._armed:
            await route.fallback()
            return
        if (
            self._http500_left > 0
            and request.resource_type == "document"
            and canonical_path(request.url) == self._http500_path
        ):
            self._http500_left -= 1
            self.injected.append(f"http500 on {self._http500_path}")
            await route.fulfill(status=500, content_type="text/html", body=_ERROR_PAGE)
            return
        if self._plan.slow_ms and request.resource_type in ("document", "xhr", "fetch"):
            await asyncio.sleep(self._plan.slow_ms / 1000)
        await route.fallback()

    async def before_step(self, step: Step) -> None:
        self._armed = True
        if self._plan.session_expire and not self._expired and step.action == "navigate":
            # The session lives only in cookies; without them the next page load is anonymous,
            # which is exactly what the app shows after a server-side timeout.
            await self._web.page.context.clear_cookies()
            self._expired = True
            self.injected.append(f"session cookies cleared before {step.id}")
        if self._plan.dialog and not self._dialog_sent and step.action in ("extract", "click"):
            await self._web.page.evaluate(
                "setTimeout(() => alert('Your session will expire soon'), 0)"
            )
            self._dialog_sent = True
            self.injected.append(f"alert() raised before {step.id}")
