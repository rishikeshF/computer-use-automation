"""Command line. The catalog arrives with its milestone.

    uv run cua discover --name get_account_balance \\
        --goal "Find the balance and available amount of an account" \\
        --input account_id=13344 --output balance --output available --login

    uv run cua replay artifacts/get_account_balance@0.2.0.json \\
        --input account_id=13344 [--inject http500] [--inject session_expire]

    uv run cua replay artifacts/open_new_account@0.1.0.json --headed --handoff ...
    uv run cua operator list | take | release | approve | decline <run>

    uv run cua seal artifacts/ensure_session@0.2.0.json     (after a reviewer edits a draft)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values

from cua.core.artifact import Capability
from cua.core.faults import FaultPlan, parse_faults
from cua.core.har import OfflinePack, leaks, sanitize, stand_ins
from cua.core.policy import Policy
from cua.core.result import BusinessOutcome, NeedsHuman, RunResult, Success
from cua.core.runlog import RunLog, new_run_id, portable, utc_now
from cua.core.template import render
from cua.core.tenant import TenantOverride, Variant, apply
from cua.core.url import canonical_path
from cua.discover.agent import Approver, DiscoveryTask, discover
from cua.discover.recorder import RecorderError, record
from cua.discover.session import login, logout
from cua.handoff import control
from cua.handoff.control import ControlError, ControlFile, Intervention, Lease
from cua.handoff.runner import HumanHandoff
from cua.llm.claude import DEFAULT_MODEL, AnthropicClient
from cua.replay import detectors
from cua.replay.engine import ReplayOptions, replay
from cua.surface.capture import HumanCapture
from cua.surface.faults import WebFaults
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

ROOT = Path(__file__).resolve().parents[2]
PARABANK = "https://parabank.parasoft.com/parabank"
VENDOR = "parasoft/parabank"

app = typer.Typer(no_args_is_help=True, add_completion=False)
operator = typer.Typer(no_args_is_help=True, help="Hand a paused run to a person, and back.")
app.add_typer(operator, name="operator")


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


def newest_usable(artifacts: Path, name: str) -> Path | None:
    """The highest version of a capability that is not deprecated. Preconditions use it."""
    for _, path in sorted(versions(artifacts, name).items(), reverse=True):
        cap = Capability.model_validate_json(path.read_text(encoding="utf-8"))
        if cap.status != "deprecated":
            return path
    return None


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
    handoff: Annotated[
        bool, typer.Option(help="Ask for approvals through `cua operator`, not the terminal.")
    ] = False,
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
    code = asyncio.run(
        _discover(task, sign_in, headed, handoff, policy, runs_dir, artifacts_dir, model)
    )
    raise typer.Exit(code)


async def _discover(
    task: DiscoveryTask,
    sign_in: bool,
    headed: bool,
    handoff: bool,
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
            approver: Approver = ask_human
            if handoff:
                desk = HumanHandoff(ControlFile.create(log.dir, run_id), log, notify=to_stderr)
                approver = operator_approver(desk, task.name)
            run = await discover(task, surface, llm, log, approver=approver)
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


def to_stderr(message: str) -> None:
    typer.echo(message, err=True)


def operator_approver(desk: HumanHandoff, name: str) -> Approver:
    """Discovery's approval question, asked through control.json: `cua operator approve`."""
    asked = 0

    async def approve(what: str) -> bool:
        nonlocal asked
        asked += 1
        iv = Intervention(
            id=f"approval-{asked}",
            kind="approval",
            capability=f"{name} (discovery)",
            intent=what,
            reason="irreversible action held for approval",
            requested_at=utc_now(),
        )
        return (await desk.request(iv)).kind == "approved"

    return approve


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
    handoff: Annotated[
        bool, typer.Option(help="Pause for a person (`cua operator`) instead of ending the run.")
    ] = False,
    handoff_timeout: Annotated[
        float, typer.Option(help="Seconds to wait for a person before giving up.")
    ] = 1800.0,
    offline: Annotated[
        Path | None, typer.Option(help="Replay from a recorded pack (offline/*.json), no network.")
    ] = None,
    record_har: Annotated[
        Path | None, typer.Option(help="Record this run as an offline pack (offline/<name>.json).")
    ] = None,
    tenant: Annotated[
        Path | None, typer.Option(help="Apply a tenant override (tenants/<tenant>.yaml).")
    ] = None,
    variant: Annotated[
        Path | None,
        typer.Option(help="Make the app look like another tenant (tenants/variants/*.yaml)."),
    ] = None,
    scrub: Annotated[
        list[str],
        typer.Option(help="More text to remove from the recording (a name shown on screen)."),
    ] = [],  # noqa: B006
    base_url: Annotated[str, typer.Option(help="App root.")] = PARABANK,
    policy: Annotated[Path, typer.Option(help="Policy file.")] = ROOT / "policies/parabank.yaml",
    runs_dir: Annotated[Path, typer.Option(help="Where the run log goes.")] = ROOT / "runs",
    artifacts_dir: Annotated[Path, typer.Option(help="Where preconditions are found.")] = ROOT
    / "artifacts",
) -> None:
    """Run an artifact with no model. Exit code: 0 success, 3 business outcome, 4 needs a human,
    1 failure. With --handoff a pause waits for a person instead of ending the run; add --headed
    so they can act in the window. With --offline, the app is a recording: no network, no .env."""
    cap = load_artifact(artifact)
    try:
        faults = parse_faults(inject)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    if offline and record_har:
        raise typer.BadParameter("--offline and --record-har cannot be combined")
    given = parse_inputs(input)
    mode = Offline()
    if offline is not None:
        mode.pack = OfflinePack.model_validate_json(offline.read_text(encoding="utf-8"))
        mode.har = offline.with_name(mode.pack.har)
        base_url = mode.pack.base_url
        given = {**mode.pack.inputs, **given}  # the recorded stand-ins, unless overridden
    mode.record_to = record_har
    mode.scrub = tuple(scrub)
    look = Look(
        override=TenantOverride.load(tenant) if tenant else None,
        variant=Variant.load(variant) if variant else None,
    )
    result = asyncio.run(
        _replay(
            cap,
            given,
            faults,
            confirm,
            headed,
            handoff_timeout if handoff else None,
            base_url,
            policy,
            runs_dir,
            artifacts_dir,
            mode,
            look,
        )
    )
    typer.echo(json.dumps(json.loads(result.model_dump_json()), indent=2))
    codes = {Success: 0, BusinessOutcome: 3, NeedsHuman: 4}
    raise typer.Exit(codes.get(type(result), 1))


@dataclass
class Look:
    """Which tenant the run is for, and (for demos) which tenant the app is made to look like."""

    override: TenantOverride | None = None
    variant: Variant | None = None

    def effective(self, cap: Capability) -> tuple[Capability, list[str]]:
        return apply(cap, self.override) if self.override else (cap, [])

    def signals(self) -> detectors.AppSignals:
        base = detectors.PARABANK
        if self.override is None or not self.override.signals:
            return base
        return detectors.AppSignals(**{**base.__dict__, **self.override.signals})


@dataclass
class Offline:
    """How a replay talks to the app: live (all None), from a pack, or live while recording one."""

    pack: OfflinePack | None = None
    har: Path | None = None
    record_to: Path | None = None  # the pack to write; the .har goes next to it
    scrub: tuple[str, ...] = ()  # not inputs, but not to be kept either: the customer's name


async def _replay(
    cap: Capability,
    inputs: dict[str, str],
    faults_plan: FaultPlan,
    confirm: bool,
    headed: bool,
    handoff_timeout: float | None,  # None: no handoff
    base_url: str,
    policy_file: Path,
    runs_dir: Path,
    artifacts_dir: Path,
    mode: Offline,
    look: Look,
) -> RunResult:
    pol = Policy.load(policy_file)
    if look.override is not None:
        pol = pol.renamed(look.override.text)  # renamed controls keep their risk
    base_cap = cap
    cap, renamed = look.effective(cap)
    # Preconditions are the newest artifact of that name. Their inputs come from the environment
    # as PARABANK_<INPUT>, so credentials never appear on a command line or in shell history.
    pre: dict[str, tuple[Capability, dict[str, str]]] = {}
    for name in cap.preconditions:
        found = newest_usable(artifacts_dir, name)
        if found is None:
            raise typer.BadParameter(f"precondition {name!r} has no usable artifact")
        pre_cap, pre_renamed = look.effective(load_artifact(found))
        renamed += [f"{name}: {r}" for r in pre_renamed]
        if mode.pack is not None:
            pre_inputs = dict(mode.pack.preconditions.get(name, {}))  # recorded stand-ins
        else:
            pre_inputs = {i.name: env(f"PARABANK_{i.name.upper()}") or "" for i in pre_cap.inputs}
        pre[name] = (pre_cap, pre_inputs)

    declared = [i.name for i in cap.inputs if i.sensitive]
    sensitive = pol.sensitive_values(inputs, declared)
    for pre_cap, pre_inputs in pre.values():
        sensitive |= pol.sensitive_values(
            pre_inputs, [i.name for i in pre_cap.inputs if i.sensitive]
        )

    run_id = new_run_id(f"replay-{cap.name}")
    # A recording starts raw (passwords, cookies). It goes to a temp dir, never into the repo,
    # and only its sanitized copy is kept.
    raw = Path(tempfile.mkdtemp(prefix="cua-har-")) / "raw.har" if mode.record_to else None
    async with WebSurface.launch(
        headless=not headed,
        allowed_url=pol.url_allowed,
        har=mode.har or raw,
        record_har=raw is not None,
        variant=look.variant.text if look.variant else None,
    ) as web:
        # With a handoff, control.json says who acts; the surface checks it before each action.
        lease: Lease | None = None
        if handoff_timeout is not None:
            lease = Lease(ControlFile.create(runs_dir / run_id, run_id))
        surface = GuardedSurface(web, pol, sensitive=sensitive, gate=lease)
        log = RunLog(runs_dir, run_id, surface.redact, base=ROOT)
        if mode.pack is not None and mode.har is not None:
            log.event("offline", pack=mode.pack.har, recorded_at=mode.pack.recorded_at)
        if look.variant is not None:
            log.event("variant", tenant=look.variant.tenant, note=look.variant.note)
        if look.override is not None:
            log.event(
                "tenant_override",
                tenant=look.override.tenant,
                base=base_cap.id,
                base_hash=base_cap.content_hash,
                effective=cap.id,
                effective_hash=cap.content_hash,
                renamed=renamed,
            )
        desk: HumanHandoff | None = None
        if lease is not None and handoff_timeout is not None:
            capture = HumanCapture(
                web, pol, surface.redact, log, human_holds=lease.control.human_holds
            )
            await capture.install()
            desk = HumanHandoff(
                lease.control,
                log,
                window=capture,
                notify=to_stderr,
                timeout_s=handoff_timeout,
                lease=lease,
            )
            if not headed:
                log.event("warning", message="handoff without --headed: approve or decline only")
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
                opts = ReplayOptions(
                    base_url=base_url,
                    handoff=desk,
                    signals=look.signals(),
                    check_fingerprint=False,
                )
                return await replay(pre_cap, pre_inputs, surface, log, opts)

            return establish

        options = ReplayOptions(
            base_url=base_url,
            confirm=confirm,
            preconditions={name: handler(name) for name in pre},
            hooks=injector,
            handoff=desk,
            signals=look.signals(),
        )
        try:
            result = await replay(cap, inputs, surface, log, options)
        finally:
            if injector is not None:
                log.event("faults_injected", injected=injector.summary())
            await logout(surface)
        log.event("result", result=result.model_dump(mode="json"))
        log.result(result)
    if raw is not None and mode.record_to is not None:
        pre_inputs_by_name = {name: inputs for name, (_, inputs) in pre.items()}
        write_pack(
            raw, mode.record_to, cap, base_url, inputs, pre_inputs_by_name, sensitive, mode.scrub
        )
    return result


def write_pack(
    raw: Path,
    pack_path: Path,
    cap: Capability,
    base_url: str,
    inputs: dict[str, str],
    pre_inputs: dict[str, dict[str, str]],
    sensitive: dict[str, str],
    scrub: tuple[str, ...],
) -> None:
    """Sanitize the raw recording into offline/<name>.har + .json, or write nothing."""
    try:
        text = raw.read_text(encoding="utf-8")
        fakes = stand_ins(sensitive, text)
        replace = {sensitive[n]: fakes[n] for n in sensitive} | {v: "Customer" for v in scrub}
        clean = sanitize(json.loads(text), replace)
        left = leaks(clean, [*sensitive.values(), *scrub])
        if left:
            typer.echo("Recording still holds sensitive values; nothing written.", err=True)
            return
        pack_path.parent.mkdir(parents=True, exist_ok=True)
        har_path = pack_path.with_suffix(".har")
        har_path.write_text(json.dumps(clean, indent=1, ensure_ascii=False), encoding="utf-8")
        pack = OfflinePack(
            har=har_path.name,
            capability=cap.id,
            base_url=base_url,
            recorded_at=utc_now(),
            inputs={k: fakes.get(k, v) for k, v in inputs.items()},
            preconditions={
                name: {k: fakes.get(k, v) for k, v in vals.items()}
                for name, vals in pre_inputs.items()
            },
            note="Sensitive values are stand-ins. Offline: no network, no .env needed.",
        )
        pack_path.write_text(pack.model_dump_json(indent=2) + "\n", encoding="utf-8")
        typer.echo(f"Offline pack: {portable(pack_path, ROOT)} (+ {har_path.name})", err=True)
    finally:
        raw.unlink(missing_ok=True)
        raw.parent.rmdir()


@app.command("seal")
def seal_cmd(artifact: Annotated[Path, typer.Argument(help="Draft artifact JSON file.")]) -> None:
    """Recompute content_hash after a reviewer edited a draft (added an outcome, fixed a
    locator). Only drafts: changing an approved artifact means a new version."""
    cap = Capability.model_validate_json(artifact.read_text(encoding="utf-8"))
    if cap.status != "draft":
        raise typer.BadParameter(f"{artifact.name} is {cap.status}; only drafts can be re-sealed")
    problems = cap.problems()
    if problems:
        raise typer.BadParameter("; ".join(problems))
    sealed = cap.sealed()
    artifact.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(f"{artifact.name}: {cap.content_hash} -> {sealed.content_hash}")


# ---- operator: the person's side of a handoff ---------------------------------------------------

RunsDir = Annotated[Path, typer.Option(help="Where runs are (evidence for demo runs).")]
By = Annotated[str, typer.Option(help="Who is acting, for the evidence.")]


def find_run(run: str, runs_dir: Path) -> ControlFile:
    """A run by its directory, or by any unique part of its id."""
    if (Path(run) / "control.json").exists():
        return ControlFile(Path(run))
    dirs = [d for d in runs_dir.glob("*") if (d / "control.json").exists() and run in d.name]
    if len(dirs) != 1:
        found = ", ".join(d.name for d in dirs) or "none"
        raise typer.BadParameter(f"{run!r} matches {len(dirs)} handoff runs: {found}")
    return ControlFile(dirs[0])


def operate(run: str, runs_dir: Path, change: control.Change, verb: str) -> None:
    cf = find_run(run, runs_dir)
    try:
        state = cf.update(change)
    except ControlError as e:
        typer.echo(f"{cf.dir.name}: {e}", err=True)
        raise typer.Exit(2) from e
    typer.echo(f"{cf.dir.name}: {verb}; {state.holder} holds control (epoch {state.epoch})")


@operator.command("list")
def op_list(runs_dir: RunsDir = ROOT / "runs") -> None:
    """Runs waiting for a person, or held by one."""
    for path in sorted(runs_dir.glob("*/control.json")):
        s = ControlFile(path.parent).read()
        if s.intervention is None and s.owner == "automation":
            continue
        iv = s.intervention
        what = f"{iv.id}: {iv.kind} at {iv.step_id or '-'}: {iv.reason}" if iv else "no request"
        typer.echo(f"{s.run_id}  held by {s.holder}  epoch {s.epoch}  {what}")


@operator.command("take")
def op_take(run: str, runs_dir: RunsDir = ROOT / "runs", by: By = "operator") -> None:
    """Take the live browser window. Automation stops before its next action."""
    operate(run, runs_dir, control.take(by), "taken")


@operator.command("release")
def op_release(run: str, runs_dir: RunsDir = ROOT / "runs", by: By = "operator") -> None:
    """Give control back. The run re-checks the page against the artifact, then resumes."""
    operate(run, runs_dir, control.release(by), "released")


@operator.command("approve")
def op_approve(
    run: str,
    intervention: Annotated[str, typer.Argument(help="Id of the request, e.g. s5-1.")],
    runs_dir: RunsDir = ROOT / "runs",
    by: By = "operator",
) -> None:
    """Approve the waiting request by its id. Automation does that step, and only that step."""
    operate(run, runs_dir, control.approve(by, intervention), "approved")


@operator.command("decline")
def op_decline(
    run: str,
    intervention: Annotated[str, typer.Argument(help="Id of the request, e.g. s5-1.")],
    runs_dir: RunsDir = ROOT / "runs",
    by: By = "operator",
) -> None:
    """Say no to the waiting request by its id. The run ends without doing the step."""
    operate(run, runs_dir, control.decline(by, intervention), "declined")


if __name__ == "__main__":
    app()
