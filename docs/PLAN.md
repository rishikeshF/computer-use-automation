# PLAN.md

## 0. Decisions (defend these)

| Decision | Choice | Why |
|---|---|---|
| Target | Public ParaBank | Real legacy JSP banking app, built for automation, zero legal risk, showcaseable |
| Offline mode | Playwright HAR record/replay | README requires "run without live services"; no Docker needed |
| Fault injection | Playwright route interception | Client-side only, never stresses their server, deterministic |
| Perception | Accessibility snapshot (frame-aware) + screenshot | Works without test IDs; same model exists on desktop (UIA / macOS AX) |
| Action targeting | Model acts on snapshot refs; recorder turns refs into ranked locator chains | Model never writes selectors, so artifacts stay robust and reviewable |
| LLM | Claude Sonnet via tool calling, behind `LLMClient` protocol | Strong at tool use; swap to OpenAI by adapter |
| Architecture | Single process, asyncio, files on disk | Brief rewards simplicity; seams designed so they could scale |
| Risky actions | Block in discovery until a human approves; in replay require artifact `approved` + caller `confirm=true` | Irreversible money movement must never be implicit |

## 1. Capabilities to build

1. `get_account_balance(account_id) -> {balance, available}` (read-only)
   - Business outcome: `account_not_found`
2. `open_new_account(account_type, funding_account_id) -> {new_account_id}` (irreversible, gated)
   - Business outcome: whatever ParaBank shows on invalid input (find out during discovery)
3. `ensure_session(username, password)` login sub-capability
   - Reused as the recovery handler for session expiry
   - Credentials come from env, marked secret

## 2. Repo layout

```
src/cua/
  core/
    artifact.py     # Pydantic schema (the focal point)
    result.py       # RunResult union
    policy.py       # allowlist, risk classes, check()
    redact.py
  surface/
    base.py         # Surface protocol: observe(), resolve(LocatorChain), act(Action)
    web.py          # Playwright implementation; desktop would be another impl
    snapshot.js     # extracts interactive + text nodes: role, name, label, frame path, bbox
  discover/
    agent.py        # observe -> decide -> act loop
    tools.py        # tool schemas given to the LLM
    recorder.py     # trace -> artifact draft
  replay/
    engine.py       # deterministic state machine
    detectors.py    # outcome detectors (app-level + capability-level)
    faults.py       # route-interception fault injection
  handoff/
    control.py      # control lease (owner, epoch) in runs/<id>/control.json
    capture.py      # injected listeners recording human actions
    operator.py     # CLI: list / take / release
  llm/              # LLMClient protocol + anthropic/openai adapters
  cli.py            # discover, replay, operator, catalog
policies/parabank.yaml
artifacts/          # versioned capability JSON
evidence/           # committed demo runs
tests/
```

## 3. Artifact schema (sketch, refine in M3)

```
Capability
  id, name, version (semver), content_hash, status: draft | approved
  description            # human + agent readable
  app: {vendor: "parasoft/parabank", app_version_fingerprint, base_url_param}
  inputs:  [{name, type, required, sensitive, pattern}]
  outputs: [{name, type, source_step}]
  preconditions: [requires: "ensure_session"]
  steps: [Step]
  success: Checkpoint
  outcomes: [OutcomeDetector]    # business outcomes this capability can return
  recovery: [RecoveryHandler]    # dismiss dialog, re-login, retry load
  risk: read_only | reversible | irreversible
  provenance: {discovered_at, model, run_id, reviewed_by}
  overrides: {}                  # per-tenant / per-version patches (design for 3.7)

Step
  id, intent (plain English), action: click | fill | select | navigate | extract | wait
  target: LocatorChain
  value: literal | {param: "account_id"}
  pre: Checkpoint | null
  post: Checkpoint               # verified after every step, not assumed
  risk, timeout_ms, retry: {max, backoff_ms, on: [transient]}

LocatorChain (ordered, first unique match wins, record which one matched)
  1. role + accessible name (+ frame path)
  2. label-relative ("textbox labelled 'Amount'")
  3. text anchor + relative position ("button right of 'From account'")
  4. css (last resort, flagged as fragile in review)
  5. bbox (only for non-DOM surfaces, never auto-used on web)

Checkpoint
  url_pattern (canonicalized, /activity.htm?id=:account_id), landmark_text, element_present
```

## 4. Result contract

```
Success(outputs, evidence)
BusinessOutcome(code, message, step_id, evidence)   # e.g. account_not_found
NeedsHuman(reason, intervention_id, step_id)        # paused, session kept alive
Failure(kind, step_id, expected, observed, screenshot, trace)
  kind: locator_not_found | checkpoint_mismatch | app_error | policy_violation | timeout
```

Recoverable conditions never reach the caller as a status. They appear in `evidence.recoveries[]`.

## 5. Milestones (review after each)

### M1. Surface + snapshot
`observe()` returns refs, roles, names and a screenshot for ParaBank pages. `resolve()` turns a LocatorChain into a unique element or a typed error.

**Check:** a script prints the snapshot of the login page and the accounts overview.

### M2. Policy + redaction (early, because everything calls it)
- `policies/parabank.yaml` defines allowed origins and paths, allowed actions, and irreversible patterns (buttons like Transfer, Open New Account, Pay, Apply Now).
- `redact()` handles secrets, account numbers, SSN-shaped strings and passwords.

**Check:**
- Unit tests show navigation to admin.htm is blocked.
- Unit tests show an irreversible click without approval is blocked.

### M3. Discovery agent + recorder
- The loop takes a goal plus explicit inputs (`--input account_id=13344`). The model binds typed values to param names, so we never guess what to parameterize.
- Tools: click, fill, select, navigate, extract, done, give_up, request_human.
- Stop conditions: max steps, timeout, and repeated identical observations (stuck).
- The recorder emits a draft artifact.

**Check:**
- A real run on public ParaBank produces `artifacts/get_account_balance@0.1.0.json`.
- The log lands in `evidence/`.

### M4. Replay engine + detectors + faults
- Runs a state machine over steps and verifies `post` after each one.
- App-level detectors:
  - session expired (login form present)
  - app error page ("internal error")
  - unexpected dialog
- Capability-level detectors cover business outcomes.
- Faults are flags: `--inject slow|http500|session_expire|dialog`.

**Check:** replays exist for each of these, saved in `evidence/`:
- success
- bad account id
- injected 500
- session expiry recovered through ensure_session

### M5. Human handoff
- Run headed and keep the same browser.
- The control lease in `runs/<id>/control.json` tracks owner (automation | human) and epoch. Automation checks the lease before every action.
- An intervention request carries the capability, step, reason, redacted screenshot and URL.
- `cua operator take <run>` hands the human the live window.
- Capture listeners log human clicks and inputs (values redacted).
- `cua operator release <run>` re-observes, checks the checkpoint, and resumes.

**Check:** the evidence shows the irreversible step in `open_new_account` pausing, a human approving or acting, and the run completing.

### M6. Offline mode + evidence + docs
- HAR recording for offline replay.
- README demo path.
- REPORT.md using their 7 exact headings.

**Check:** a fresh clone can run replay offline in under 5 minutes.

### M7. Stretch (pick one or two)
- **Agent-facing catalog as an MCP server:** each approved artifact becomes a typed tool. Show Claude calling `get_account_balance`. This is the best demo for other startups.
- **Tenant B variant:** route interception rewrites ParaBank labels and branding ("Open New Account" becomes "Create Account"). The same artifact replays with an `overrides` patch. This is the cross-tenant story without Docker.

## 6. Known risks
- **Public site downtime or DB resets:** HAR offline mode, plus a `seed` command that registers a fresh fake user.
- **ParaBank account ids change after resets:** discover account ids at runtime, never hardcode them in artifacts.
- **Snapshot quality on table-heavy pages:** include table cell text with row and column headers in the snapshot.
