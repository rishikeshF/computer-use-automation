"""Safety policy: what the system may touch, how risky each action is, and what is sensitive.

Every action in discovery, replay and handoff goes through `Policy.check()` before it runs.
The policy is plain data (policies/<app>.yaml) so a reviewer at the bank can read and change it
without reading code.

Three questions, answered in order:
  1. Is this allowed at all?     allowlist of origins, paths and action kinds; denied controls
  2. How risky is it?            read_only | reversible | irreversible
  3. Is risk covered?            irreversible needs explicit approval, otherwise it is not run

Risk is decided conservatively: a button we have no rule for is irreversible by default. In a
bank, an unknown button that submits a form is more likely to move money than not, and the cost of
asking a human once is much smaller than the cost of an unwanted transfer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from cua.core.action import Action, Navigate
from cua.core.url import canonical_path, origin

Risk = Literal["read_only", "reversible", "irreversible"]


class TargetInfo(Protocol):
    """What the policy needs to know about a control. A surface Node satisfies this; core does
    not import the surface layer."""

    @property
    def role(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def label(self) -> str | None: ...


Verdict = Literal["allow", "deny", "needs_approval"]


class ControlRule(BaseModel):
    """Matches a control by role (optional) and a case-insensitive full-match regex on its name."""

    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    name: str
    _rx: re.Pattern[str] = PrivateAttr()

    def model_post_init(self, _: object) -> None:
        self._rx = re.compile(self.name, re.IGNORECASE)

    def matches(self, target: TargetInfo) -> bool:
        return (self.role is None or self.role == target.role) and bool(
            self._rx.fullmatch(target.name or target.label or "")
        )


class RiskRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    irreversible: list[ControlRule] = []
    reversible: list[ControlRule] = []
    default_button: Risk = "irreversible"


class PolicyConfig(BaseModel):
    """The YAML file, validated. Unknown keys are an error: a typo must not weaken the policy."""

    model_config = ConfigDict(extra="forbid")

    name: str
    origins: list[str] = Field(min_length=1)
    allow_paths: list[str] = Field(min_length=1)
    deny_paths: list[str] = []  # deny wins over allow
    actions: list[Literal["click", "fill", "select", "navigate"]]
    deny_controls: list[ControlRule] = []
    risk: RiskRules = RiskRules()
    # Input names matching any of these are sensitive, whatever the artifact says. An artifact
    # can mark more inputs sensitive; it can never mark these as safe.
    sensitive_inputs: list[str] = []


class Decision(BaseModel):
    verdict: Verdict
    risk: Risk
    rule: str  # which rule decided, e.g. "deny_paths:/parabank/admin.htm", for evidence
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class Policy:
    def __init__(self, config: PolicyConfig):
        self.config = config
        self._origins = {o.rstrip("/").lower() for o in config.origins}
        self._sensitive = [re.compile(p, re.IGNORECASE) for p in config.sensitive_inputs]

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        return cls(PolicyConfig.model_validate(yaml.safe_load(Path(path).read_text("utf-8"))))

    # ---- 1. allowed? ---------------------------------------------------------------------------

    def url_verdict(self, url: str) -> tuple[bool, str]:
        """(allowed, rule) for a URL. Used for navigation, for the page we are acting on, and by
        the surface's network guard so even a click we misjudged cannot load a denied page."""
        o = origin(url)
        if o is None or o not in self._origins:
            return False, f"origins:{o or url.split(':', 1)[0] + ':'}"
        path = canonical_path(url)
        for pattern in self.config.deny_paths:
            if fnmatchcase(path, pattern.lower()):
                return False, f"deny_paths:{pattern}"
        for pattern in self.config.allow_paths:
            if fnmatchcase(path, pattern.lower()):
                return True, f"allow_paths:{pattern}"
        return False, "allow_paths:(no match)"

    def url_allowed(self, url: str) -> bool:
        return self.url_verdict(url)[0]

    # ---- 2. how risky? -------------------------------------------------------------------------

    def classify(self, action: Action, target: TargetInfo | None) -> tuple[Risk, str]:
        """(risk, rule). Only clicks commit anything; typing and selecting stay in the form."""
        if isinstance(action, Navigate):
            return "read_only", "navigate"
        if action.kind in ("fill", "select"):
            return "reversible", action.kind
        if target is None:
            return "irreversible", "unknown target"  # cannot see what we click: assume the worst
        for rule in self.config.risk.irreversible:
            if rule.matches(target):
                return "irreversible", f"risk.irreversible:{rule.name}"
        for rule in self.config.risk.reversible:
            if rule.matches(target):
                return "reversible", f"risk.reversible:{rule.name}"
        if target.role == "button":
            return self.config.risk.default_button, "risk.default_button"
        if target.role == "link":
            return "read_only", "link"
        return "reversible", f"role:{target.role}"

    # ---- the check -----------------------------------------------------------------------------

    def check(
        self,
        action: Action,
        *,
        url: str,
        target: TargetInfo | None = None,
        irreversible_approved: bool = False,
    ) -> Decision:
        """Decide whether `action` may run now, on page `url`, against `target`.

        `irreversible_approved` is the caller's statement that a human approved this step in
        discovery/handoff, or that the artifact is approved and the caller passed confirm=true in
        replay. The policy does not trust it for anything except irreversible risk.
        """

        def deny(rule: str, reason: str, risk: Risk = "read_only") -> Decision:
            return Decision(verdict="deny", risk=risk, rule=rule, reason=reason)

        if action.kind not in self.config.actions:
            return deny("actions", f"action kind {action.kind!r} is not allowed")

        if isinstance(action, Navigate):
            ok, rule = self.url_verdict(action.url)
            if not ok:
                return deny(rule, f"navigation to {canonical_path(action.url)} is outside policy")
        else:
            # Acting on a page we should never have reached is denied, whatever the control is.
            ok, rule = self.url_verdict(url)
            if not ok:
                return deny(rule, f"current page {canonical_path(url)} is outside policy")
            if target is not None:
                for control in self.config.deny_controls:
                    if control.matches(target):
                        return deny(
                            f"deny_controls:{control.name}", f"control {target.name!r} is denied"
                        )

        risk, rule = self.classify(action, target)
        if risk == "irreversible" and not irreversible_approved:
            return Decision(
                verdict="needs_approval",
                risk=risk,
                rule=rule,
                reason=f"irreversible {action.kind} needs explicit approval",
            )
        return Decision(verdict="allow", risk=risk, rule=rule, reason="within policy")

    # ---- sensitive inputs ----------------------------------------------------------------------

    def is_sensitive(self, input_name: str) -> bool:
        return any(rx.search(input_name) for rx in self._sensitive)

    def sensitive_values(
        self, inputs: Mapping[str, str], declared: Iterable[str] = ()
    ) -> dict[str, str]:
        """The subset of `inputs` that must be redacted in logs and masked in screenshots:
        everything the policy says is sensitive, plus whatever the artifact declared."""
        declared = set(declared)
        return {k: v for k, v in inputs.items() if v and (k in declared or self.is_sensitive(k))}
