"""Primitive UI actions, addressed by snapshot ref.

These are what the surface executes. Discovery produces them from model tool calls, replay
produces them from artifact steps after resolving a LocatorChain to a ref, and the human handoff
captures them. Policy (M2) checks every one of them before it runs.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Action(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Click(_Action):
    kind: Literal["click"] = "click"
    ref: str


class Fill(_Action):
    kind: Literal["fill"] = "fill"
    ref: str
    # Hidden from repr so a stray print or log line never shows a typed password.
    value: str = Field(repr=False)
    sensitive: bool = False


class Select(_Action):
    kind: Literal["select"] = "select"
    ref: str
    option: str  # visible option text, which is what the model and a human see


class Navigate(_Action):
    kind: Literal["navigate"] = "navigate"
    url: str


Action = Annotated[Click | Fill | Select | Navigate, Field(discriminator="kind")]
