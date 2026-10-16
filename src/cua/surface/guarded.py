"""GuardedSurface: the only surface discovery, replay and handoff are given.

It wraps a raw Surface and makes two rules structural instead of a convention callers can forget:
  - Every action is checked by the policy first. Nothing runs unless the verdict is "allow".
  - Every screenshot masks the run's sensitive values.

act() returns the Decision instead of raising, because a blocked action is information the caller
acts on (deny -> policy_violation failure, needs_approval -> ask a human), not a crash. Either
way the action did not run, so ignoring the return value is still safe.
"""

from __future__ import annotations

from collections.abc import Mapping

from cua.core.action import Action, Navigate
from cua.core.locator import LocatorChain
from cua.core.policy import Decision, Policy
from cua.core.redact import Redactor
from cua.surface.base import Node, Observation, Resolved, Surface, Unresolved


class GuardedSurface:
    def __init__(
        self, surface: Surface, policy: Policy, *, sensitive: Mapping[str, str] | None = None
    ):
        self._surface = surface
        self.policy = policy
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

    async def act(self, action: Action, *, irreversible_approved: bool = False) -> Decision:
        """Check the action against policy, run it only if allowed, return the decision."""
        target = None if isinstance(action, Navigate) else self._seen.get(action.ref)
        decision = self.policy.check(
            action, url=self.url, target=target, irreversible_approved=irreversible_approved
        )
        if decision.allowed:
            await self._surface.act(action)
        return decision
