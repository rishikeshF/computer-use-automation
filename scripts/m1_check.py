"""M1 acceptance check: print the snapshot of the ParaBank login page and the accounts overview.

Logs in by resolving LocatorChains (not hard-coded refs), the same way replay will, so this also
shows resolve() working on real legacy markup: the login inputs have no <label>, so the RoleName
strategy finds nothing and the LabelText fallback fires.

    uv run python scripts/m1_check.py [--headed]

Needs `username` and `password` in .env. Screenshots go to runs/m1/ (gitignored).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from cua.core.action import Click, Fill, Navigate
from cua.core.locator import LabelText, LocatorChain, RoleName, TextAnchor
from cua.surface.base import Observation, Resolved, Unresolved, render
from cua.surface.web import WebSurface

BASE = "https://parabank.parasoft.com/parabank"
OUT = Path("runs/m1")

# Chains a recorder would emit for the login form. Primary strategy first, fallbacks after.
USERNAME = LocatorChain(
    strategies=(
        RoleName(role="textbox", name="Username"),
        LabelText(role="textbox", label="Username"),
        TextAnchor(role="textbox", anchor="Username", direction="below"),
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


def credentials() -> tuple[str | None, str | None]:
    """ParaBank test credentials. Read from the .env file itself, never via os.environ for the
    plain names: Windows env vars are case-insensitive, so `username` would silently resolve to
    the OS login name in %USERNAME%."""
    file = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    user = (
        os.environ.get("PARABANK_USERNAME") or file.get("PARABANK_USERNAME") or file.get("username")
    )
    pw = (
        os.environ.get("PARABANK_PASSWORD") or file.get("PARABANK_PASSWORD") or file.get("password")
    )
    return user, pw


def show(title: str, obs: Observation, shot: str) -> None:
    print(f"\n===== {title} ({len(obs.nodes)} nodes) =====")
    print(render(obs))
    if obs.screenshot:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / shot).write_bytes(obs.screenshot)
        print(f"(screenshot: {OUT / shot}, password fields masked)")


def explain(what: str, r: Resolved | Unresolved) -> str:
    tried = ", ".join(f"{a.kind}={a.matches}" for a in r.attempts)
    if isinstance(r, Resolved):
        print(f"resolve {what:<10} -> {r.ref} via strategy #{r.strategy_index} [{tried}]")
        return r.ref
    print(f"resolve {what:<10} -> UNRESOLVED ({r.reason}) [{tried}]")
    raise SystemExit(1)


async def main(headed: bool) -> int:
    user, password = credentials()
    if not user or not password:
        print("Set PARABANK_USERNAME and PARABANK_PASSWORD (or username/password) in .env")
        return 2

    async with WebSurface.launch(headless=not headed) as s:
        await s.act(Navigate(url=f"{BASE}/index.htm"))
        login = await s.observe()
        show("LOGIN PAGE", login, "login.png")

        print("\n===== RESOLVE (login form) =====")
        await s.act(
            Fill(ref=explain("username", await s.resolve(USERNAME)), value=user, sensitive=True)
        )
        await s.act(
            Fill(ref=explain("password", await s.resolve(PASSWORD)), value=password, sensitive=True)
        )
        await s.act(Click(ref=explain("log in", await s.resolve(LOG_IN))))

        overview = await s.observe()
        show("ACCOUNTS OVERVIEW", overview, "overview.png")

        # Pass/fail. A failed login is a business outcome ParaBank reports as page text.
        ok = True
        if not overview.find("heading", "Accounts Overview"):
            errors = [
                n.name
                for n in overview.nodes
                if "could not be verified" in n.name or "error" in n.name.lower()
            ]
            print(f"\nFAIL: did not reach Accounts Overview. Page says: {errors or overview.title}")
            ok = False
        accounts = [
            n for n in overview.nodes if n.role == "link" and n.table and n.table.col == "Account"
        ]
        balances = [
            n
            for n in overview.nodes
            if n.role == "cell" and n.table and n.table.col and "Balance" in n.table.col
        ]
        if ok and not accounts:
            print("\nFAIL: no account links found in the accounts table")
            ok = False
        if ok:
            print(
                f"\nFound {len(accounts)} account(s); balance cells carry row context, e.g. "
                f"{balances[0].name!r} in row {balances[0].table.row!r}"
                if balances and balances[0].table
                else f"\nFound {len(accounts)} account(s)"
            )
            # Show that an account link recorded by its name resolves uniquely on the live page.
            explain(
                "account",
                await s.resolve(
                    LocatorChain(strategies=(RoleName(role="link", name=accounts[0].name),))
                ),
            )

        # Log out to leave the shared demo the way we found it.
        out = await s.resolve(LOG_OUT)
        if isinstance(out, Resolved):
            await s.act(Click(ref=out.ref))

    print("\nM1 CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Windows consoles default to cp1252
    sys.exit(asyncio.run(main(headed="--headed" in sys.argv)))
