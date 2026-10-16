"""Scripted ParaBank login, used to start a discovery run already signed in.

A stand-in for the `ensure_session` capability. Once replay exists (M4), discovery will replay
the recorded ensure_session artifact instead, and this file goes away. It already works the way
replay will: it finds the controls by LocatorChain, and every action goes through policy.
"""

from __future__ import annotations

from cua.core.action import Click, Fill, Navigate
from cua.core.locator import LabelText, LocatorChain, RoleName
from cua.surface.base import Resolved
from cua.surface.guarded import GuardedSurface

USERNAME = LocatorChain(
    strategies=(
        RoleName(role="textbox", name="Username"),
        LabelText(role="textbox", label="Username"),
    )
)
PASSWORD = LocatorChain(
    strategies=(
        RoleName(role="textbox", name="Password"),
        LabelText(role="textbox", label="Password"),
    )
)
LOG_IN = LocatorChain(strategies=(RoleName(role="button", name="Log In"),))
LOG_OUT = LocatorChain(strategies=(RoleName(role="link", name="Log Out"),))


async def login(surface: GuardedSurface, base_url: str, username: str, password: str) -> bool:
    """Sign in. True when the page afterwards offers Log Out (a business 'no' returns False)."""
    await surface.act(Navigate(url=f"{base_url.rstrip('/')}/index.htm"))
    if isinstance(await surface.resolve(LOG_OUT), Resolved):
        return True  # already signed in
    for chain, value in ((USERNAME, username), (PASSWORD, password)):
        found = await surface.resolve(chain, timeout_ms=5_000)
        if not isinstance(found, Resolved):
            return False
        await surface.act(Fill(ref=found.ref, value=value, sensitive=True))
    button = await surface.resolve(LOG_IN)
    if not isinstance(button, Resolved):
        return False
    decision = await surface.act(Click(ref=button.ref))
    return decision.allowed and isinstance(
        await surface.resolve(LOG_OUT, timeout_ms=5_000), Resolved
    )


async def logout(surface: GuardedSurface) -> None:
    """Leave the shared demo the way we found it. Best effort."""
    found = await surface.resolve(LOG_OUT)
    if isinstance(found, Resolved):
        await surface.act(Click(ref=found.ref))
