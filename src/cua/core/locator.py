"""How a recorded step finds its target control again on replay.

A LocatorChain is an ordered list of strategies. On replay the surface tries them in order and the
first strategy with exactly one match wins. The chain records *what a human would use to find the
control* (its role and name, the text next to it), not where it sits in the markup, because legacy
apps have no test IDs and their markup is the least stable thing about them.

Strategies, most robust first:
  1. RoleName      role + accessible name ("button 'Log In'")
  2. LabelText     role + the visible text that labels it when the markup does not
                   ("textbox labelled 'Username'"; legacy forms rarely use <label>)
  3. TextAnchor    role + direction from a unique piece of text ("combobox right of 'Type'")
  4. Css           last resort, flagged as fragile in review
  5. BBox          screen position; only meaningful for surfaces with no element tree
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class TextAnchor(_Strategy):
    kind: Literal["text_anchor"] = "text_anchor"
    role: str
    anchor: str
    direction: Literal["right_of", "left_of", "below", "above"]


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
    RoleName | LabelText | TextAnchor | Css | BBox, Field(discriminator="kind")
]


class LocatorChain(BaseModel):
    """Ordered fallbacks for one control. First strategy with a unique match wins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame: FramePath = ()
    strategies: tuple[LocatorStrategy, ...] = Field(min_length=1)
