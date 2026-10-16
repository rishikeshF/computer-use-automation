"""Command line: `cua discover` now; replay, operator and catalog arrive with their milestones.

    uv run cua discover --name get_account_balance \\
        --goal "Find the balance and available amount of an account" \\
        --input account_id=13344 --output balance --output available --login
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values

from cua.core.policy import Policy
from cua.core.result import Success
from cua.core.runlog import RunLog, new_run_id
from cua.discover.agent import DiscoveryTask, discover
from cua.discover.recorder import RecorderError, record
from cua.discover.session import login, logout
from cua.llm.claude import DEFAULT_MODEL, AnthropicClient
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

ROOT = Path(__file__).resolve().parents[2]
PARABANK = "https://parabank.parasoft.com/parabank"
VENDOR = "parasoft/parabank"

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Computer-use automation: discover a flow once with an LLM, replay it without one."""


def env(name: str) -> str | None:
    """From the environment, else the repo's .env. Names are namespaced (PARABANK_USERNAME, not
    username) because Windows env vars are case-insensitive and %USERNAME% always exists."""
    return os.environ.get(name) or dotenv_values(ROOT / ".env").get(name)


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    """`name=value`, or `name=env:VAR` to read the value from the environment / .env."""
    out: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise typer.BadParameter(f"expected name=value with a snake_case name, got {pair!r}")
        if value.startswith("env:"):
            resolved = env(value[4:])
            if not resolved:
                raise typer.BadParameter(f"{value[4:]} is not set (environment or .env)")
            value = resolved
        out[name] = value
    return out


def next_version(artifacts: Path, name: str) -> str:
    """0.1.0 for a new capability; otherwise the next minor after the highest on disk."""
    found = [
        tuple(int(x) for x in m.groups())
        for p in artifacts.glob(f"{name}@*.json")
        if (m := re.fullmatch(rf"{re.escape(name)}@(\d+)\.(\d+)\.(\d+)\.json", p.name))
    ]
    if not found:
        return "0.1.0"
    major, minor, _ = max(found)
    return f"{major}.{minor + 1}.0"


async def ask_human(what: str) -> bool:
    """Approval prompt on the terminal. No terminal means no human, so the answer is no."""
    if not sys.stdin.isatty():
        typer.echo(f"approval needed for: {what} (no terminal, declined)")
        return False
    return await asyncio.to_thread(
        typer.confirm, f"\nApprove irreversible action: {what}?", default=False
    )


@app.command("discover")
def discover_cmd(
    name: Annotated[str, typer.Option(help="Capability name, snake_case.")],
    goal: Annotated[str, typer.Option(help="What the model should achieve, in plain English.")],
    input: Annotated[list[str], typer.Option("--input", help="name=value or name=env:VAR")] = [],  # noqa: B006
    output: Annotated[
        list[str], typer.Option("--output", help="Output the run must extract.")
    ] = [],  # noqa: B006
    sign_in: Annotated[
        bool, typer.Option("--login", help="Start signed in (PARABANK_* creds).")
    ] = False,
    headed: Annotated[bool, typer.Option(help="Show the browser.")] = False,
    base_url: Annotated[str, typer.Option(help="App root.")] = PARABANK,
    policy: Annotated[Path, typer.Option(help="Policy file.")] = ROOT / "policies/parabank.yaml",
    runs_dir: Annotated[Path, typer.Option(help="Where the run log goes.")] = ROOT / "runs",
    artifacts_dir: Annotated[Path, typer.Option(help="Where the draft artifact goes.")] = ROOT
    / "artifacts",
    model: Annotated[str, typer.Option(help="Model id.")] = DEFAULT_MODEL,
    max_steps: Annotated[int, typer.Option(help="Action limit.")] = 25,
    timeout: Annotated[float, typer.Option(help="Seconds before the run is stopped.")] = 300.0,
) -> None:
    """Let the model work out a task once, under policy, and record it as a draft artifact."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise typer.BadParameter("name must be snake_case")
    inputs = parse_inputs(input)
    task = DiscoveryTask(
        name=name,
        goal=goal,
        inputs=inputs,
        outputs=output,
        base_url=base_url,
        max_steps=max_steps,
        timeout_s=timeout,
    )
    code = asyncio.run(_discover(task, sign_in, headed, policy, runs_dir, artifacts_dir, model))
    raise typer.Exit(code)


async def _discover(
    task: DiscoveryTask,
    sign_in: bool,
    headed: bool,
    policy_file: Path,
    runs_dir: Path,
    artifacts_dir: Path,
    model: str,
) -> int:
    pol = Policy.load(policy_file)
    sensitive = pol.sensitive_values(task.inputs)
    creds: tuple[str, str] | None = None
    if sign_in:
        user, pw = env("PARABANK_USERNAME"), env("PARABANK_PASSWORD")
        if not user or not pw:
            typer.echo("Set PARABANK_USERNAME and PARABANK_PASSWORD in .env to use --login")
            return 2
        creds = (user, pw)
        sensitive |= {"parabank_username": user, "parabank_password": pw}

    run_id = new_run_id(f"discover-{task.name}")
    async with WebSurface.launch(headless=not headed, allowed_url=pol.url_allowed) as web:
        surface = GuardedSurface(web, pol, sensitive=sensitive)
        log = RunLog(runs_dir, run_id, surface.redact)
        try:
            if creds:
                ok = await login(surface, task.base_url, *creds)
                log.event("bootstrap_login", ok=ok)
                if not ok:
                    typer.echo("Login failed: the test user may be gone (the public DB resets).")
                    return 1
            llm = AnthropicClient(
                model,
                api_key=env("ANTHROPIC_API_KEY"),
                workspace_id=env("ANTHROPIC_WORKSPACE_ID"),
            )
            run = await discover(task, surface, llm, log, approver=ask_human)
        finally:
            await logout(surface)

    typer.echo(json.dumps(surface.redact.data(run.result), indent=2))
    if not isinstance(run.result, Success):
        return 1
    try:
        cap = record(
            run.trace,
            name=task.name,
            version=next_version(artifacts_dir, task.name),
            goal=task.goal,
            description=task.goal,
            inputs=task.inputs,
            sensitive={k for k in task.inputs if k in sensitive},
            base_url=task.base_url,
            vendor=VENDOR,
            model=model,
            run_id=run_id,
            preconditions=["ensure_session"] if sign_in else [],
        )
    except RecorderError as e:
        log.event("recorder_failed", error=str(e))
        typer.echo(f"Recorder refused to write the artifact: {e}")
        return 1
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"{cap.id}.json"
    path.write_text(cap.model_dump_json(indent=2) + "\n", encoding="utf-8")
    log.event("artifact", path=str(path), content_hash=cap.content_hash, steps=len(cap.steps))
    typer.echo(f"\nDraft artifact: {path}\nEvidence: {log.dir}")
    return 0


if __name__ == "__main__":
    app()
