"""The tools the model gets during discovery, and their validated inputs.

The model acts on refs from the latest observation ("[e12] button 'Log In'"). It never writes a
selector: the recorder turns each ref into a LocatorChain afterwards, so the artifact holds a
human description of the control, not whatever markup the model happened to see.

Every action tool takes an `intent`: one plain-English sentence that becomes the step's intent in
the artifact. That is what a reviewer at the bank reads, so the model writes it at the moment it
knows why it is acting.

Values are literal text or `{{input_name}}`. The model never sees a sensitive input's value, so
`{{password}}` is the only way it can type one.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cua.llm.base import ToolSpec

_INTENT = {
    "type": "string",
    "description": "One plain-English sentence: why this step is taken. Goes in the artifact.",
}
_REF = {
    "type": "string",
    "description": "Ref of the control, e.g. e12, from the latest observation.",
}


def _schema(**props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


TOOLS = [
    ToolSpec(
        name="click",
        description="Click a link, button, checkbox or radio by ref.",
        input_schema=_schema(ref=_REF, intent=_INTENT),
    ),
    ToolSpec(
        name="fill",
        description=(
            "Type into a text field, replacing what is there. `value` is literal text or "
            "{{input_name}} for one of the task inputs. Always use {{input_name}} when typing "
            "an input's value, and it is the only way to type a sensitive input."
        ),
        input_schema=_schema(ref=_REF, value={"type": "string"}, intent=_INTENT),
    ),
    ToolSpec(
        name="select",
        description="Choose an option in a dropdown by its visible text, or {{input_name}}.",
        input_schema=_schema(ref=_REF, option={"type": "string"}, intent=_INTENT),
    ),
    ToolSpec(
        name="navigate",
        description=(
            "Go to a URL inside the app. Prefer clicking links: a person would, and the "
            "recorded flow is more robust for it. Use {{base_url}} for the app root."
        ),
        input_schema=_schema(url={"type": "string"}, intent=_INTENT),
    ),
    ToolSpec(
        name="extract",
        description=(
            "Read the text of one node on screen as a task output. Pick the node that holds "
            "the value itself (e.g. the cell with the amount), not its label."
        ),
        input_schema=_schema(
            ref=_REF,
            output={"type": "string", "description": "Output name, snake_case, e.g. balance."},
            intent=_INTENT,
        ),
    ),
    ToolSpec(
        name="done",
        description="The goal is achieved and every output is extracted. Ends the run.",
        input_schema=_schema(summary={"type": "string"}),
    ),
    ToolSpec(
        name="give_up",
        description=(
            "The goal cannot be achieved (blocked, missing data, app error). Ends the run. "
            "If the app itself says the request is invalid (e.g. no such account), that is "
            "a finding: describe exactly what the page says."
        ),
        input_schema=_schema(reason={"type": "string"}),
    ),
    ToolSpec(
        name="request_human",
        description=(
            "Pause for a person: a CAPTCHA, an ambiguous choice you should not make alone, "
            "or anything you are unsure is safe. Ends the run as needs_human."
        ),
        input_schema=_schema(reason={"type": "string"}),
    ),
]

ACTION_TOOLS = {"click", "fill", "select", "navigate", "extract"}


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionIn(_Input):
    """Every action carries the plain-English intent that becomes the step's intent."""

    intent: str = Field(min_length=1)


class ClickIn(ActionIn):
    ref: str


class FillIn(ActionIn):
    ref: str
    value: str


class SelectIn(ActionIn):
    ref: str
    option: str


class NavigateIn(ActionIn):
    url: str


class ExtractIn(ActionIn):
    ref: str
    output: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")


class DoneIn(_Input):
    summary: str


class ReasonIn(_Input):
    reason: str


INPUTS: dict[str, type[_Input]] = {
    "click": ClickIn,
    "fill": FillIn,
    "select": SelectIn,
    "navigate": NavigateIn,
    "extract": ExtractIn,
    "done": DoneIn,
    "give_up": ReasonIn,
    "request_human": ReasonIn,
}
