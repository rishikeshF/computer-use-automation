"""The Surface contract: how the rest of the system perceives and acts on a UI.

Everything above this layer (discovery, replay, handoff) talks to a Surface and never to a browser
or OS automation library directly. A web app, a legacy web app and a desktop app differ only in
how they produce Nodes and execute Actions; recorded flows only ever see Nodes and LocatorChains.

    observe()  -> Observation   what is on screen now, as role/name nodes with refs + a screenshot
    resolve()  -> Resolved | Unresolved
                                turn a recorded LocatorChain into a live ref, or say why not
    act()      -> None          execute one Action against a ref (raises SurfaceError if it cannot)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cua.core.action import Action
from cua.core.locator import FramePath, LocatorChain


class TableContext(BaseModel):
    """Where a node sits in a data table. Lets the model read "Balance for account 13344"."""

    model_config = ConfigDict(frozen=True)

    row: str | None = None  # text of the first cell in the node's row
    col: str | None = None  # text of the header cell above the node's column


class Node(BaseModel):
    """One interactive control or piece of visible text, as a human operator would describe it."""

    model_config = ConfigDict(frozen=True)

    ref: str  # handle for act(); valid while the element exists on the page
    role: str  # link, button, textbox, combobox, checkbox, heading, cell, text, ...
    name: str  # accessible name, or the text itself for text nodes
    label: str | None = None  # visible text labelling an unnamed control (legacy forms)
    value: str | None = None  # current value; always None for secret fields
    options: tuple[str, ...] = ()  # visible option texts, for selects
    states: tuple[str, ...] = ()  # disabled, checked, required, secret
    table: TableContext | None = None
    frame: FramePath = ()
    bbox: tuple[float, float, float, float]  # x, y, width, height in frame document coords


class Observation(BaseModel):
    url: str
    title: str
    nodes: tuple[Node, ...]
    # Frames we could not read (detached mid-scan, navigating). Reported, not hidden.
    skipped_frames: tuple[str, ...] = ()
    # PNG bytes with secret fields masked. Kept out of serialized logs; saved as a file instead.
    screenshot: bytes | None = Field(default=None, repr=False, exclude=True)

    def find(self, role: str, name: str) -> list[Node]:
        return [n for n in self.nodes if n.role == role and n.name == name]


class StrategyAttempt(BaseModel):
    """What one strategy in a chain matched. Kept for evidence: which fallback fired and why."""

    index: int
    kind: str
    matches: int
    note: str | None = None


class Resolved(BaseModel):
    ref: str
    strategy_index: int  # 0 means the primary strategy matched; higher means a fallback fired
    attempts: tuple[StrategyAttempt, ...]
    # The matched control as perceived. None only for a css match on a non-snapshot element.
    node: Node | None = None


class Unresolved(BaseModel):
    reason: Literal["not_found", "ambiguous", "frame_not_found"]
    attempts: tuple[StrategyAttempt, ...]


class SurfaceError(Exception):
    """An action could not be executed. Hard failure at this layer; callers decide what it means."""

    def __init__(self, kind: Literal["stale_ref", "not_actionable", "navigation"], detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class Surface(Protocol):
    @property
    def url(self) -> str: ...

    async def observe(
        self, *, screenshot: bool = True, mask_values: Sequence[str] = ()
    ) -> Observation: ...

    async def resolve(
        self, chain: LocatorChain, *, timeout_ms: int = 0
    ) -> Resolved | Unresolved: ...

    async def act(self, action: Action) -> None: ...


def render(obs: Observation) -> str:
    """Compact text form of an observation. This is what the model reads in discovery."""
    lines = [f"URL: {obs.url}", f"TITLE: {obs.title}"]
    for n in obs.nodes:
        parts = [f"[{n.ref}]", n.role]
        if n.name:
            parts.append(f'"{n.name}"')
        if n.label:
            parts.append(f'label="{n.label}"')
        if n.value:
            parts.append(f'value="{n.value}"')
        if n.options:
            parts.append("options=[" + ", ".join(f'"{o}"' for o in n.options) + "]")
        if n.states:
            parts.append("(" + ", ".join(n.states) + ")")
        if n.table and (n.table.row or n.table.col):
            where = [
                f'row="{n.table.row}"' if n.table.row else "",
                f'col="{n.table.col}"' if n.table.col else "",
            ]
            parts.append("{" + " ".join(w for w in where if w) + "}")
        if n.frame:
            parts.append(f"frame={'/'.join(n.frame)}")
        lines.append(" ".join(parts))
    if obs.skipped_frames:
        lines.append(f"SKIPPED FRAMES: {', '.join(obs.skipped_frames)}")
    return "\n".join(lines)
