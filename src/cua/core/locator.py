"""How a recorded step finds its target control again on replay.

A LocatorChain is an ordered list of strategies. On replay the surface tries them in order and the
first strategy with exactly one match wins. The chain records *what a human would use to find the
control* (its role and name, the text next to it), not where it sits in the markup, because legacy
apps have no test IDs and their markup is the least stable thing about them.

Strategies, most robust first:
  1. RoleName      role + accessible name ("button 'Log In'")
  2. LabelText     role + the visible text that labels it when the markup does not
                   ("textbox labelled 'Username'"; legacy forms rarely use <label>)
  3. TableCell     role + the row and column it sits in ("cell in row 'Balance:'"); how a
                   value is read without recording the value itself
  4. TextAnchor    role + direction from a unique piece of text ("combobox right of 'Type'").
                   With contains=True the anchor is a short part of a longer text, so a reworded
                   or re-priced paragraph ("A minimum of $100.00 ...") can still be found
  5. Css           last resort, flagged as fragile in review
  6. BBox          screen position; only meaningful for surfaces with no element tree

Text fields may contain `{{param}}` placeholders ("link named {{account_id}}"). bind() fills them
in at replay time, so the chain on disk never holds a caller's value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from cua.core.template import render

# Path from the top-level frame to the frame holding the control. Each hop is the frame's name,
# or "#<index>" among its parent's child frames when it has no name. () is the top-level frame.
FramePath = tuple[str, ...]


class _Strategy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def fragile(self) -> bool:
        """True when the strategy depends on markup or layout a reviewer should double-check."""
        return False


class RoleName(_Strategy):
    kind: Literal["role_name"] = "role_name"
    role: str
    name: str


class LabelText(_Strategy):
    kind: Literal["label_text"] = "label_text"
    role: str
    label: str


class TableCell(_Strategy):
    """A node by its table position: the first cell of its row and the header of its column.
    Either may be None (a two-column "Label: value" table has rows but no header).

    row "*" means any row. Header cells have their own role (columnheader), so a "cell" or
    "link" with row "*" is data. Used to ask "has this column any data yet?", for example to
    prove a table finished loading before concluding that a row is missing."""

    kind: Literal["table_cell"] = "table_cell"
    role: str
    row: str | None = None
    col: str | None = None


class TextAnchor(_Strategy):
    kind: Literal["text_anchor"] = "text_anchor"
    role: str
    anchor: str
    direction: Literal["right_of", "left_of", "below", "above"]
    contains: bool = False  # anchor is part of the text, not all of it

    @model_serializer(mode="wrap")
    def _omit_default(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        # Added after the first artifacts were sealed; left out when False so they keep their hash.
        body: dict[str, object] = handler(self)
        if not self.contains:
            body.pop("contains", None)
        return body


class Css(_Strategy):
    kind: Literal["css"] = "css"
    selector: str

    @property
    def fragile(self) -> bool:
        return True


class BBox(_Strategy):
    kind: Literal["bbox"] = "bbox"
    x: float
    y: float
    width: float
    height: float

    @property
    def fragile(self) -> bool:
        return True


LocatorStrategy = Annotated[
    RoleName | LabelText | TableCell | TextAnchor | Css | BBox, Field(discriminator="kind")
]


class LocatorChain(BaseModel):
    """Ordered fallbacks for one control. First strategy with a unique match wins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame: FramePath = ()
    strategies: tuple[LocatorStrategy, ...] = Field(min_length=1)

    def bind(self, params: Mapping[str, str]) -> LocatorChain:
        """A copy with every `{{param}}` in the strategies' text replaced by its value."""
        bound = [
            s.model_copy(
                update={k: render(v, params) for k, v in s if isinstance(v, str) and k != "kind"}
            )
            for s in self.strategies
        ]
        return self.model_copy(update={"strategies": tuple(bound)})
