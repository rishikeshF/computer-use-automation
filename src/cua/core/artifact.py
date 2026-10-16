"""The capability artifact: a typed, versioned, reviewable description of how to do one task.

Discovery writes it once (with an LLM). Replay reads it forever (without one). A reviewer at the
bank reads it in between and flips `status` to approved. So it has to be three things at once:

  - Complete enough to replay with no model: every step has a target, a value and a checkpoint.
  - Safe to store and share: caller values appear only as `{{param}}` placeholders.
  - Readable by a person: every step carries the plain-English intent it was recorded with.

`content_hash` covers what the capability *does* (inputs, steps, checkpoints, outputs), not who
reviewed it or when. Two artifacts with the same hash behave the same.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.core.locator import LocatorChain
from cua.core.outputs import OutputType
from cua.core.policy import Risk
from cua.core.template import placeholders

SCHEMA_VERSION = 1
_RISK_ORDER: dict[Risk, int] = {"read_only": 0, "reversible": 1, "irreversible": 2}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InputSpec(_Model):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    type: Literal["string"] = "string"  # the UI only ever takes text; parsing is the caller's job
    required: bool = True
    sensitive: bool = False  # redacted in logs, masked in screenshots, never stored
    pattern: str | None = None  # optional regex the value must fully match, checked before replay
    description: str = ""


class OutputSpec(_Model):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    type: OutputType = "string"  # decimal: "$515.50" is returned as Decimal("515.50")
    unit: str | None = None  # e.g. USD, when the screen shows a currency sign
    source_step: str  # id of the extract step that produces it


class Checkpoint(_Model):
    """What must be true on screen at a point in the flow. Verified on replay, never assumed.

    Every field that is set must hold. `url_pattern` is a canonical path plus query, with param
    values as placeholders and other query values as `*`: /parabank/activity.htm?id={{account_id}}
    """

    url_pattern: str | None = None
    landmark_text: str | None = None  # a heading or text that identifies the page
    element_present: LocatorChain | None = None


class Condition(_Model):
    """A state of the screen, checked on replay. Holds when every part holds."""

    page: Checkpoint  # we are on the right page (so an absence below means something)
    present: tuple[LocatorChain, ...] = ()  # each resolves: the page has finished loading
    absent: tuple[LocatorChain, ...] = ()  # none resolves: the thing asked for is not there


class OutcomeDetector(_Model):
    """A business answer the app can give instead of the happy path, declared, not inferred.

    Replay checks `when` before each step in `check_before`. If it holds, the run ends as
    BusinessOutcome(code), not as a Failure. The `present` guard is what keeps the two apart:
    if the page changed shape, the guard fails too, and the run is a Failure that a person
    should look at, never a false "account not found".
    """

    code: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")  # account_not_found
    message: str
    check_before: tuple[str, ...] = Field(min_length=1)
    when: Condition
    rationale: str = ""  # why the recorder (or a reviewer) believes this, for review


class ParamValue(_Model):
    """A step value taken from a caller input at replay time."""

    param: str


StepValue = Annotated[str | ParamValue, Field(union_mode="left_to_right")]


class Retry(_Model):
    max: int = 0
    backoff_ms: int = 500
    on: tuple[Literal["transient"], ...] = ("transient",)


class Step(_Model):
    id: str
    intent: str  # plain English, written by the model at discovery time, read by reviewers
    action: Literal["click", "fill", "select", "navigate", "extract"]
    target: LocatorChain | None = None  # None only for navigate
    # fill: the text. select: the option text. navigate: the URL (may use {{base_url}}).
    # extract: the output name the read value is stored under.
    value: StepValue | None = None
    pre: Checkpoint | None = None
    post: Checkpoint = Checkpoint()
    risk: Risk = "read_only"
    timeout_ms: int = 10_000
    retry: Retry = Retry()
    # Locators the recorder could not prove unique on the recorded page. A reviewer should look.
    review_notes: tuple[str, ...] = ()


class AppInfo(_Model):
    vendor: str
    app_version_fingerprint: str | None = None  # filled in once replay can fingerprint the app
    base_url_param: str = "base_url"


class Provenance(_Model):
    discovered_at: str  # ISO 8601, UTC
    model: str
    run_id: str
    goal: str
    reviewed_by: str | None = None


class Capability(_Model):
    schema_version: int = SCHEMA_VERSION
    id: str  # "<name>@<version>"
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    content_hash: str = ""
    status: Literal["draft", "approved"] = "draft"
    description: str
    app: AppInfo
    inputs: tuple[InputSpec, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    preconditions: tuple[str, ...] = ()  # names of capabilities that must hold first
    steps: tuple[Step, ...] = Field(min_length=1)
    success: Checkpoint
    outcomes: tuple[OutcomeDetector, ...] = ()
    recovery: tuple[dict[str, object], ...] = ()  # recovery handlers, typed in M4
    risk: Risk
    provenance: Provenance
    overrides: dict[str, object] = {}  # per-tenant / per-version patches

    def behavior_hash(self) -> str:
        """sha256 over what the capability does. Excludes identity, status and provenance, so
        approving or re-versioning an artifact does not change it."""
        body = self.model_dump(
            mode="json",
            exclude={"id", "version", "content_hash", "status", "provenance"},
        )
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def sealed(self) -> Capability:
        """A copy with content_hash set. Call after the last change, before writing to disk."""
        return self.model_copy(update={"content_hash": self.behavior_hash()})

    def problems(self) -> list[str]:
        """Consistency checks a schema cannot express. Empty means the artifact is coherent."""
        found: list[str] = []
        names = {i.name for i in self.inputs} | {self.app.base_url_param}
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            found.append("duplicate step ids")
        used = placeholders(self.model_dump_json(include={"steps", "success", "outcomes"}))
        for name in sorted(used - names):
            found.append(f"placeholder {{{{{name}}}}} is not a declared input")
        for s in self.steps:
            if isinstance(s.value, ParamValue) and s.value.param not in names:
                found.append(f"step {s.id} uses undeclared input {s.value.param!r}")
            if s.action != "navigate" and s.target is None:
                found.append(f"step {s.id} ({s.action}) has no target")
        extracts = {s.id for s in self.steps if s.action == "extract"}
        for o in self.outputs:
            if o.source_step not in extracts:
                found.append(f"output {o.name!r} comes from {o.source_step!r}, not an extract step")
        for d in self.outcomes:
            for sid in d.check_before:
                if sid not in step_ids:
                    found.append(f"outcome {d.code!r} is checked before unknown step {sid!r}")
        worst = max_risk([s.risk for s in self.steps])
        if _RISK_ORDER[worst] > _RISK_ORDER[self.risk]:
            found.append(f"capability risk {self.risk!r} is lower than its riskiest step ({worst})")
        return found


def max_risk(risks: list[Risk]) -> Risk:
    return max(risks, key=_RISK_ORDER.__getitem__, default="read_only")
