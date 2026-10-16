"""GuardedSurface: the only surface discovery, replay and handoff are given.

It wraps a raw Surface and makes three rules structural instead of a convention callers can forget:
  - Every action is checked by the policy first. Nothing runs unless the verdict is "allow".
  - Every screenshot masks the run's sensitive values.
  - When a person can take over the run (human handoff), every action first checks that
    automation still holds control. If not, it raises ControlLost and nothing runs.

act() returns the Decision instead of raising, because a blocked action is information the caller
acts on (deny -> policy_violation failure, needs_approval -> ask a human), not a crash. Either
way the action did not run, so ignoring the return value is still safe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from cua.core.action import Action, Navigate
from cua.core.locator import LocatorChain
from cua.core.policy import Decision, Policy
from cua.core.redact import Redactor
from cua.surface.base import Node, Observation, Resolved, Surface, Unresolved


class ControlLost(Exception):
    """A person holds control of the run (or did since automation last checked). The action did
    not run."""


class ControlGate(Protocol):
    """Who may act right now. The handoff lease implements it; raises ControlLost if not us."""

    def check(self) -> None: ...


class GuardedSurface:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        *,
        sensitive: Mapping[str, str] | None = None,
        gate: ControlGate | None = None,
    ):
        self._surface = surface
        self.policy = policy
        self._gate = gate
        self._sensitive = dict(sensitive or {})
        self.sensitive_names = frozenset(self._sensitive)  # names only; values stay private
        # The run's redactor: knows this run's secret values by name. All logging goes through it.
        self.redact = Redactor(self._sensitive)
        self._seen: dict[str, Node] = {}  # what each ref looked like when last perceived

    @property
    def url(self) -> str:
        return self._surface.url

    async def observe(self, *, screenshot: bool = True) -> Observation:
        obs = await self._surface.observe(
            screenshot=screenshot, mask_values=tuple(self._sensitive.values())
        )
        self._seen.update({n.ref: n for n in obs.nodes})
        return obs

    async def resolve(self, chain: LocatorChain, *, timeout_ms: int = 0) -> Resolved | Unresolved:
        result = await self._surface.resolve(chain, timeout_ms=timeout_ms)
        if isinstance(result, Resolved) and result.node is not None:
            self._seen[result.ref] = result.node
        return result

    async def act(
        self,
        action: Action,
        *,
        irreversible_approved: bool = False,
        before_run: Callable[[Decision], None] | None = None,
    ) -> Decision:
        """Check the action against policy, run it only if allowed, return the decision.

        `before_run` is called once the action is allowed, just before it runs. Replay uses it to
        mark an irreversible step as committed first: if the surface then raises halfway (the
        click landed, the page never answered), the run already knows it may have happened."""
        if self._gate is not None:
            self._gate.check()  # before policy: a person at the controls outranks any verdict
        target = None if isinstance(action, Navigate) else self._seen.get(action.ref)
        decision = self.policy.check(
            action, url=self.url, target=target, irreversible_approved=irreversible_approved
        )
        if decision.allowed:
            if before_run is not None:
                before_run(decision)
            await self._surface.act(action)
        return decision
