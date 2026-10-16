"""Command line. Operator and catalog arrive with their milestones.

    uv run cua discover --name get_account_balance \\
        --goal "Find the balance and available amount of an account" \\
        --input account_id=13344 --output balance --output available --login

    uv run cua replay artifacts/get_account_balance@0.2.0.json \\
        --input account_id=13344 [--inject http500] [--inject session_expire]
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

from cua.core.artifact import Capability
from cua.core.faults import FaultPlan, parse_faults
from cua.core.policy import Policy
from cua.core.result import BusinessOutcome, NeedsHuman, RunResult, Success
from cua.core.runlog import RunLog, new_run_id, portable
from cua.core.template import render
from cua.core.url import canonical_path
from cua.discover.agent import DiscoveryTask, discover
from cua.discover.recorder import RecorderError, record
from cua.discover.session import login, logout
from cua.llm.claude import DEFAULT_MODEL, AnthropicClient
from cua.replay.engine import ReplayOptions, replay
from cua.surface.faults import WebFaults
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


def versions(artifacts: Path, name: str) -> dict[tuple[int, int, int], Path]:
    """Every artifact file of a capability on disk, by semantic version."""
    found: dict[tuple[int, int, int], Path] = {}
    for p in artifacts.glob(f"{name}@*.json"):
        m = re.fullmatch(rf"{re.escape(name)}@(\d+)\.(\d+)\.(\d+)\.json", p.name)
        if m:
            found[(int(m[1]), int(m[2]), int(m[3]))] = p
    return found


def next_version(artifacts: Path, name: str) -> str:
    """0.1.0 for a new capability; otherwise the next minor after the highest on disk."""
    found = versions(artifacts, name)
    if not found:
        return "0.1.0"
    major, minor, _ = max(found)
    return f"{major}.{minor + 1}.0"


def load_artifact(path: Path) -> Capability:
    """Read an artifact and refuse it if its content no longer matches its hash: someone
    edited the steps by hand after review, and what runs must be what was reviewed."""
    cap = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    if cap.content_hash != cap.behavior_hash():
        raise typer.BadParameter(f"{path.name}: content_hash does not match its content")
    problems = cap.problems()
    if problems:
        raise typer.BadParameter(f"{path.name}: " + "; ".join(problems))
    return cap


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
        log = RunLog(runs_dir, run_id, surface.redact, base=ROOT)
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
    log.event(
        "artifact", path=portable(path, ROOT), content_hash=cap.content_hash, steps=len(cap.steps)
    )
    typer.echo(f"\nDraft artifact: {portable(path, ROOT)}\nEvidence: {log.ref}")
    return 0


@app.command("replay")
def replay_cmd(
    artifact: Annotated[Path, typer.Argument(help="Artifact JSON file.")],
    input: Annotated[list[str], typer.Option("--input", help="name=value or name=env:VAR")] = [],  # noqa: B006
    inject: Annotated[
        list[str], typer.Option("--inject", help="slow[:ms] http500[:n] session_expire dialog")
    ] = [],  # noqa: B006
    confirm: Annotated[
        bool, typer.Option(help="Allow an approved irreversible capability.")
    ] = False,
    headed: Annotated[bool, typer.Option(help="Show the browser.")] = False,
    base_url: Annotated[str, typer.Option(help="App root.")] = PARABANK,
    policy: Annotated[Path, typer.Option(help="Policy file.")] = ROOT / "policies/parabank.yaml",
    runs_dir: Annotated[Path, typer.Option(help="Where the run log goes.")] = ROOT / "runs",
    artifacts_dir: Annotated[Path, typer.Option(help="Where preconditions are found.")] = ROOT
    / "artifacts",
) -> None:
    """Run an artifact with no model. Exit code: 0 success, 3 business outcome, 4 needs a human,
    1 failure."""
    cap = load_artifact(artifact)
    try:
        faults = parse_faults(inject)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    result = asyncio.run(
        _replay(
            cap,
            parse_inputs(input),
            faults,
            confirm,
            headed,
            base_url,
            policy,
            runs_dir,
            artifacts_dir,
        )
    )
    typer.echo(json.dumps(json.loads(result.model_dump_json()), indent=2))
    codes = {Success: 0, BusinessOutcome: 3, NeedsHuman: 4}
    raise typer.Exit(codes.get(type(result), 1))


async def _replay(
    cap: Capability,
    inputs: dict[str, str],
    faults_plan: FaultPlan,
    confirm: bool,
    headed: bool,
    base_url: str,
    policy_file: Path,
    runs_dir: Path,
    artifacts_dir: Path,
) -> RunResult:
    pol = Policy.load(policy_file)
    # Preconditions are the newest artifact of that name. Their inputs come from the environment
    # as PARABANK_<INPUT>, so credentials never appear on a command line or in shell history.
    pre: dict[str, tuple[Capability, dict[str, str]]] = {}
    for name in cap.preconditions:
        found = versions(artifacts_dir, name)
        if not found:
            raise typer.BadParameter(f"precondition {name!r} has no artifact in {artifacts_dir}")
        pre_cap = load_artifact(found[max(found)])
        pre_inputs = {i.name: env(f"PARABANK_{i.name.upper()}") or "" for i in pre_cap.inputs}
        pre[name] = (pre_cap, pre_inputs)

    declared = [i.name for i in cap.inputs if i.sensitive]
    sensitive = pol.sensitive_values(inputs, declared)
    for pre_cap, pre_inputs in pre.values():
        sensitive |= pol.sensitive_values(
            pre_inputs, [i.name for i in pre_cap.inputs if i.sensitive]
        )

    run_id = new_run_id(f"replay-{cap.name}")
    async with WebSurface.launch(headless=not headed, allowed_url=pol.url_allowed) as web:
        surface = GuardedSurface(web, pol, sensitive=sensitive)
        log = RunLog(runs_dir, run_id, surface.redact, base=ROOT)
        injector: WebFaults | None = None
        if faults_plan.active:
            injector = WebFaults(web, faults_plan)
            first = cap.steps[0]
            start = (
                canonical_path(render(str(first.value), {"base_url": base_url.rstrip("/")}))
                if first.action == "navigate"
                else None
            )
            await injector.install(start)
            log.event("faults_planned", plan=faults_plan.model_dump(), start_path=start)

        def handler(name: str):  # type: ignore[no-untyped-def]
            pre_cap, pre_inputs = pre[name]

            async def establish() -> RunResult:
                return await replay(
                    pre_cap, pre_inputs, surface, log, ReplayOptions(base_url=base_url)
                )

            return establish

        options = ReplayOptions(
            base_url=base_url,
            confirm=confirm,
            preconditions={name: handler(name) for name in pre},
            hooks=injector,
        )
        try:
            result = await replay(cap, inputs, surface, log, options)
        finally:
            if injector is not None:
                log.event("faults_injected", injected=injector.injected)
            await logout(surface)
        log.event("result", result=result.model_dump(mode="json"))
        log.result(result)
    return result


if __name__ == "__main__":
    app()
