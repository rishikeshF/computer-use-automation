"""Fault plans: which failures to inject into a replay, as plain data.

The mechanics (Playwright route interception, clearing cookies) live in cua.surface.faults, the
only layer allowed to touch the browser. This file is what the CLI parses `--inject` into and
what evidence records, so a reader can see exactly what was done to the run.

All faults are client-side, in our own browser. Nothing here sends extra load to the app.

    slow[:ms]        delay every page and XHR response (default 1500 ms)
    http500[:n]      answer the first n loads of the start page with HTTP 500 (default 1)
    session_expire   drop the session cookies just before the first navigation
    dialog           pop an unexpected alert() just before the first read or click
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FaultPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slow_ms: int = 0
    http500_times: int = 0
    http500_path: str | None = None  # canonical path to fail; the start page when None
    session_expire: bool = False
    dialog: bool = False

    @property
    def active(self) -> list[str]:
        return [k for k, v in self.model_dump().items() if v and k != "http500_path"]


def parse_faults(flags: list[str]) -> FaultPlan:
    """`["http500:2", "slow"]` -> FaultPlan. Unknown names are an error, not ignored."""
    plan: dict[str, object] = {}
    for flag in flags:
        name, _, arg = flag.partition(":")
        if name == "slow":
            plan["slow_ms"] = int(arg or 1500)
        elif name == "http500":
            plan["http500_times"] = int(arg or 1)
        elif name == "session_expire" and not arg:
            plan["session_expire"] = True
        elif name == "dialog" and not arg:
            plan["dialog"] = True
        else:
            raise ValueError(
                f"unknown fault {flag!r}; use slow[:ms], http500[:n], session_expire, dialog"
            )
    return FaultPlan.model_validate(plan)
