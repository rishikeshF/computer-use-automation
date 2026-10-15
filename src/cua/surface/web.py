"""Web surface: the only module in the system that touches Playwright.

Perception runs snapshot.js in every frame and merges the results. Resolution re-runs the same
snapshot and matches the recorded LocatorChain against it (see matching.py), so a control is found
on replay by the same description the model saw during discovery.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page, Route, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from cua.core.action import Action, Click, Fill, Navigate, Select
from cua.core.locator import BBox, Css, FramePath, LocatorChain, TextAnchor
from cua.core.url import strip_session
from cua.surface.base import (
    Node,
    Observation,
    Resolved,
    StrategyAttempt,
    SurfaceError,
    TableContext,
    Unresolved,
)
from cua.surface.matching import anchor_note, match_nodes

_SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text(encoding="utf-8")
_REF_ATTR = "data-cua-ref"
_MAX_TEXT = 200
_POLL_S = 0.25

# Tags every element showing one of the given values, so the screenshot can mask it.
_TAG_MASKS_JS = """(values) => {
  const hit = (s) => !!s && values.some((v) => s.includes(v));
  let count = 0;
  for (const el of document.querySelectorAll("*")) {
    const t = el.tagName;
    const shows = t === "INPUT" || t === "TEXTAREA" ? hit(el.value)
      : t === "SELECT" ? [...el.selectedOptions].some((o) => hit(o.text))
      : [...el.childNodes].some((c) => c.nodeType === 3 && hit(c.textContent));
    if (shows) { el.setAttribute("data-cua-mask", ""); count++; }
  }
  return count;
}"""
_CLEAR_MASKS_JS = """() => document.querySelectorAll("[data-cua-mask]")
  .forEach((e) => e.removeAttribute("data-cua-mask"))"""


class WebSurface:
    def __init__(self, page: Page, *, action_timeout_ms: int = 10_000, polite_delay_ms: int = 300):
        self._page = page
        self._action_timeout_ms = action_timeout_ms
        # Small pause after every action. We share a public demo server; be a gentle client.
        self._polite_delay_s = polite_delay_ms / 1000
        self._next_ref = 1  # refs are unique for the life of this surface, across navigations
        self._ref_frames: dict[str, Frame] = {}
        self.blocked_navigations: list[str] = []  # evidence: loads the network guard stopped

    @classmethod
    @asynccontextmanager
    async def launch(
        cls,
        *,
        headless: bool = True,
        slow_mo_ms: int = 0,
        polite_delay_ms: int = 300,
        allowed_url: Callable[[str], bool] | None = None,
    ) -> AsyncIterator[WebSurface]:
        """Start a fresh browser and yield a surface on its single page. Closes everything after.

        Pass `allowed_url` (normally `policy.url_allowed`) to install the network guard."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
            try:
                context = await browser.new_context(viewport={"width": 1280, "height": 900})
                page = await context.new_page()
                surface = cls(page, polite_delay_ms=polite_delay_ms)
                if allowed_url is not None:
                    await surface.guard_navigation(allowed_url)
                yield surface
            finally:
                await browser.close()

    @property
    def url(self) -> str:
        return self._page.url

    async def guard_navigation(self, allowed: Callable[[str], bool]) -> None:
        """Abort any page or frame load the policy forbids, before the request leaves the browser.

        Defense in depth under policy.check(): a link whose name looked harmless, a redirect, a
        script, or a human in the handoff window still cannot load a denied page."""

        async def handle(route: Route) -> None:
            request = route.request
            if request.resource_type == "document" and not allowed(request.url):
                self.blocked_navigations.append(strip_session(request.url))
                await route.abort("blockedbyclient")
            else:
                await route.fallback()

        # Context level, so pages opened by target=_blank are covered too.
        await self._page.context.route("**/*", handle)

    # ---- observe -------------------------------------------------------------------------------

    async def observe(
        self, *, screenshot: bool = True, mask_values: Sequence[str] = ()
    ) -> Observation:
        """Snapshot every frame. `mask_values` are sensitive strings (input params) to black out
        wherever they appear in the screenshot; secret fields are always masked."""
        await self._settle()
        nodes: list[Node] = []
        skipped: list[str] = []
        for frame in self._page.frames:
            path = _frame_path(frame)
            try:
                nodes += await self._scan(frame, path)
            except PlaywrightError:
                # Frame detached or navigating mid-scan. Report it rather than fail the observation.
                skipped.append("/".join(path) or "(top)")
        shot = await self._screenshot(mask_values) if screenshot else None
        return Observation(
            url=self._page.url,
            title=await self._page.title(),
            nodes=tuple(nodes),
            skipped_frames=tuple(skipped),
            screenshot=shot,
        )

    async def _settle(self) -> None:
        """Wait for the page to finish loading, including the XHR that fills legacy tables."""
        await self._page.wait_for_load_state("load")
        # A page that polls forever never goes idle, but it is still observable.
        with suppress(PlaywrightTimeout):
            await self._page.wait_for_load_state("networkidle", timeout=3_000)

    async def _scan(self, frame: Frame, path: FramePath) -> list[Node]:
        raw: dict[str, Any] = await frame.evaluate(
            _SNAPSHOT_JS, {"start": self._next_ref, "maxText": _MAX_TEXT}
        )
        self._next_ref = int(raw["next"])
        nodes = []
        for n in raw["nodes"]:
            table = TableContext(**n["table"]) if n["table"] else None
            node = Node(
                ref=n["ref"],
                role=n["role"],
                name=n["name"],
                label=n["label"],
                value=n["value"],
                options=tuple(n["options"]),
                states=tuple(n["states"]),
                table=table,
                frame=path,
                bbox=tuple(n["bbox"]),
            )
            self._ref_frames[node.ref] = frame
            nodes.append(node)
        return nodes

    async def _screenshot(self, mask_values: Sequence[str]) -> bytes | None:
        """Full-page PNG with secret fields and every element showing a sensitive value masked.

        Fails closed: if a frame cannot be tagged we cannot prove the values are hidden, so no
        screenshot is taken. Missing evidence is better than leaked evidence."""
        values = [v for v in mask_values if v]
        frames = self._page.frames
        try:
            if values:
                for f in frames:
                    await f.evaluate(_TAG_MASKS_JS, values)
            masks = [f.locator("input[type=password], [data-cua-mask]") for f in frames]
            return await self._page.screenshot(full_page=True, mask=masks)
        except PlaywrightError:
            return None
        finally:
            if values:
                for f in frames:
                    with suppress(PlaywrightError):
                        await f.evaluate(_CLEAR_MASKS_JS)

    # ---- resolve -------------------------------------------------------------------------------

    async def resolve(self, chain: LocatorChain, *, timeout_ms: int = 0) -> Resolved | Unresolved:
        """Find the unique live element for a chain. Polls up to timeout_ms if it is missing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            result = await self._resolve_once(chain)
            if isinstance(result, Resolved) or loop.time() >= deadline:
                return result
            await asyncio.sleep(_POLL_S)

    async def _resolve_once(self, chain: LocatorChain) -> Resolved | Unresolved:
        frame = next((f for f in self._page.frames if _frame_path(f) == chain.frame), None)
        if frame is None:
            return Unresolved(reason="frame_not_found", attempts=())
        try:
            nodes = await self._scan(frame, chain.frame)
        except PlaywrightError:
            return Unresolved(reason="frame_not_found", attempts=())

        attempts: list[StrategyAttempt] = []
        for i, strategy in enumerate(chain.strategies):
            note: str | None = None
            if isinstance(strategy, BBox):
                refs: list[str] = []
                note = "bbox is never used on web; the element tree is always available"
            elif isinstance(strategy, Css):
                refs = await self._match_css(frame, strategy.selector)
            else:
                refs = [n.ref for n in match_nodes(strategy, nodes)]
                if not refs and isinstance(strategy, TextAnchor):
                    note = anchor_note(strategy, nodes)
            attempts.append(
                StrategyAttempt(index=i, kind=strategy.kind, matches=len(refs), note=note)
            )
            if len(refs) == 1:
                node = next((n for n in nodes if n.ref == refs[0]), None)
                return Resolved(ref=refs[0], strategy_index=i, attempts=tuple(attempts), node=node)

        reason = "ambiguous" if any(a.matches > 1 for a in attempts) else "not_found"
        return Unresolved(reason=reason, attempts=tuple(attempts))

    async def _match_css(self, frame: Frame, selector: str) -> list[str]:
        """Visible elements matching a CSS selector, tagged with refs so act() can reach them."""
        try:
            tagged: list[str] = await frame.evaluate(
                """([selector, start]) => {
                    let next = start;
                    return [...document.querySelectorAll(selector)]
                      .filter((el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      })
                      .map((el) => el.dataset.cuaRef || (el.dataset.cuaRef = "e" + next++));
                }""",
                [selector, self._next_ref],
            )
        except PlaywrightError:
            return []  # invalid selector reads as "no match", the attempt note shows 0 matches
        for ref in tagged:
            self._ref_frames[ref] = frame
            if ref.startswith("e") and ref[1:].isdigit():
                self._next_ref = max(self._next_ref, int(ref[1:]) + 1)
        return tagged

    # ---- act -----------------------------------------------------------------------------------

    async def act(self, action: Action) -> None:
        t = self._action_timeout_ms
        try:
            match action:
                case Navigate(url=url):
                    await self._page.goto(url, wait_until="load", timeout=t)
                case Click(ref=ref):
                    await (await self._locate(ref)).click(timeout=t)
                case Fill(ref=ref, value=value):
                    await (await self._locate(ref)).fill(value, timeout=t)
                case Select(ref=ref, option=option):
                    await (await self._locate(ref)).select_option(label=option, timeout=t)
        except PlaywrightTimeout as e:
            raise SurfaceError("not_actionable", f"{action.kind} timed out after {t} ms") from e
        except PlaywrightError as e:
            if isinstance(action, Navigate):
                raise SurfaceError("navigation", str(e).splitlines()[0]) from e
            raise SurfaceError("not_actionable", str(e).splitlines()[0]) from e
        await asyncio.sleep(self._polite_delay_s)

    async def _locate(self, ref: str) -> Locator:
        frame = self._ref_frames.get(ref)
        if frame is None or frame.is_detached():
            raise SurfaceError("stale_ref", f"{ref} was never observed or its frame is gone")
        loc = frame.locator(f'[{_REF_ATTR}="{ref}"]')
        if await loc.count() != 1:
            raise SurfaceError("stale_ref", f"{ref} is no longer on the page")
        return loc


def _frame_path(frame: Frame) -> FramePath:
    """Names (or #index when unnamed) from the top frame down to this one."""
    hops: list[str] = []
    while frame.parent_frame is not None:
        parent = frame.parent_frame
        hops.append(frame.name or f"#{parent.child_frames.index(frame)}")
        frame = parent
    return tuple(reversed(hops))
