"""Web surface: the only module in the system that touches Playwright.

Perception runs snapshot.js in every frame and merges the results. Resolution re-runs the same
snapshot and matches the recorded LocatorChain against it (see matching.py), so a control is found
on replay by the same description the model saw during discovery.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from cua.core.action import Action, Click, Fill, Navigate, Select
from cua.core.locator import BBox, Css, FramePath, LocatorChain, TextAnchor
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


class WebSurface:
    def __init__(self, page: Page, *, action_timeout_ms: int = 10_000, polite_delay_ms: int = 300):
        self._page = page
        self._action_timeout_ms = action_timeout_ms
        # Small pause after every action. We share a public demo server; be a gentle client.
        self._polite_delay_s = polite_delay_ms / 1000
        self._next_ref = 1  # refs are unique for the life of this surface, across navigations
        self._ref_frames: dict[str, Frame] = {}

    @classmethod
    @asynccontextmanager
    async def launch(
        cls, *, headless: bool = True, slow_mo_ms: int = 0, polite_delay_ms: int = 300
    ) -> AsyncIterator[WebSurface]:
        """Start a fresh browser and yield a surface on its single page. Closes everything after."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
            try:
                context = await browser.new_context(viewport={"width": 1280, "height": 900})
                page = await context.new_page()
                yield cls(page, polite_delay_ms=polite_delay_ms)
            finally:
                await browser.close()

    # ---- observe -------------------------------------------------------------------------------

    async def observe(self, *, screenshot: bool = True) -> Observation:
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
        shot = await self._screenshot() if screenshot else None
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

    async def _screenshot(self) -> bytes:
        # Secret fields are masked in every frame. Sensitive params get masked by the caller's
        # policy layer later (M2); this is the floor, not the whole story.
        masks = [f.locator("input[type=password]") for f in self._page.frames]
        return await self._page.screenshot(full_page=True, mask=masks)

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
                return Resolved(ref=refs[0], strategy_index=i, attempts=tuple(attempts))

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
