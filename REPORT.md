# REPORT

Target: the public ParaBank demo bank, a server-rendered JSP app with table layouts, no test ids
and unlabeled form inputs. Capabilities: `ensure_session` (sign in), `get_account_balance`
(read-only) and `open_new_account` (irreversible). Every claim below has evidence in `evidence/`.

## 1. Architecture

One Python process, asyncio, plain files: no services, queues or database. Every part that would
need to scale (surface, artifact store, operator channel) is an interface with one implementation.

```
discover (model) --trace--> recorder --artifact--> replay engine --result--> caller
                                                        |
        GuardedSurface: policy, masks, control lease ---+--- Surface (Playwright)
                                                        |
                        handoff: control.json <--> cua operator (a person)
```

![Architecture: discovery (model) records a capability artifact; replay (no model) runs it step by step and returns success, business_outcome, needs_human or failure; a person takes the same live browser through the control lease; every action passes GuardedSurface before the Playwright surface.](docs/architecture.svg)

- **Surface** (`observe`, `resolve`, `act`) is the only code that touches Playwright. Perception
  is a frame-aware snapshot of what an operator sees: role, name, inferred label, table row and
  column, box. The model acts on snapshot refs and never writes a selector.
- **GuardedSurface** is all that discovery, replay and handoff get: every action passes the
  policy and the control lease first, every screenshot is masked.
- **Discovery** is an observe, decide, act loop with Claude Sonnet via tool calling. The model
  only picks the next control; policy, approval, redaction, stop conditions and recording are
  plain code.
- **Replay** is a deterministic state machine with no model. A test walks the import graph and
  fails if replay can reach the model.

Trade-off: accessibility-style perception over screenshot coordinates. It reads frames and tables
directly and survives restyling, and desktop accessibility APIs expose the same model. Canvas-only
UIs would need the BBox strategy (section 4).

## 2. Artifact schema

`artifacts/<name>@<semver>.json`, a Pydantic model. The caller's contract: typed `inputs`
(`sensitive`, optional regex), typed `outputs` ("$515.50" returns 515.50, unit USD), declared
`outcomes` with messages, `risk`, and review `status` (`draft`, `approved`, `deprecated`). Each
step has a plain-English `intent` (written by the model while acting), an `action`, a `target`
LocatorChain, a `value` (literal or `{{param}}`), and `pre`/`post` checkpoints (URL pattern with
params, landmark heading, element).

- **Locators describe, they do not point.** Ordered fallbacks: role and name, inferred label,
  table cell by row and column, text anchor, then CSS (flagged fragile). A strategy is kept only
  if it matches exactly the recorded control on the recorded page. Text anchors must label the
  control (nearest thing on that side, within 40 px); long ones get a short-text fallback. A
  chain found by an input drops fallbacks that ignore it ("the first balance cell" would read
  the wrong account).
- **Business outcomes are declared, with guards.** `account_not_found`: on the overview, once the
  headers and a data row are present, no link named `{{account_id}}`. If the page reshapes, the
  guard fails too and the run is a Failure, never a false "not found".
  `funding_account_not_found` checks that a loaded list lacks the option.
- **No caller value is stored**: values become `{{param}}`; the recorder refuses otherwise.
- **`content_hash` covers behavior only.** Approving or deprecating does not change it; editing a
  step does, and replay refuses a mismatched hash. Every result records artifact id, status, hash.

## 3. Determinism & error handling

Every step: wait for `pre`, check declared outcomes, resolve the target (first strategy with
exactly one match; ties are reported, never guessed), act through policy, wait for `post`.
Waits poll real state with a step timeout, never a fixed sleep.

| Result | Meaning | Examples |
|---|---|---|
| `Success` | done, typed outputs | balance 515.50 |
| `BusinessOutcome` | the app said no; caller branches on `code` | `account_not_found`, `invalid_credentials` |
| `NeedsHuman` | paused on purpose | unapproved irreversible step; any failure after a commit |
| `Failure` | automation could not do its job: kind, step, expected, observed, screenshot | `locator_not_found`, `checkpoint_mismatch`, `app_error`, `action_failed` |

Recoverable conditions keep the status and are listed in `evidence.recoveries`: expired session
(replay `ensure_session`, resume from the last page load), failed or 500 page load (retried with
backoff; clicks never are), unexpected dialog (dismissed), slow load (absorbed by the waits).

Order matters. ParaBank shows the login form and "internal error" together when a session
expires, and a wrong password under an "Error!" heading. So declared outcomes are checked before
the app-error detector, and the session check before the error check.

**Irreversible steps change the rules.** A step counts as committed once policy allows it and
before it runs, so a click that lands and then errors still "may have happened". After that the
run never signs in again, rewinds, retries or clicks again; every failure becomes `NeedsHuman`
naming the step. Tests count accounts opened on a fake server: never twice.

Drift is secondary in a stable UI: fallbacks absorb small changes and evidence records which one
fired; a changed navigation fingerprint is a warning; a reshaped page fails a checkpoint.
Faults (`slow`, `http500`, `session_expire`, `dialog`) are injected in our own browser only.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The seam is the Surface protocol and its Node (role, name, label, table position,
frame path, box). Artifacts only hold descriptions of Nodes, never markup, and matching is plain
Python over Nodes. A legacy web app is this one. A desktop app would be a surface producing the
same Nodes from Windows UI Automation (or macOS accessibility); replay, policy, handoff and the
schema do not change. A surface with no element tree would use the `BBox` strategy, which exists
in the schema and is refused on web.

**Tenants.** Tenants of one vendor product differ in words, not flows. So an artifact is
recorded once and each tenant gets one app-wide override (`tenants/tenant_b.yaml`): UI text as
the base knows it, mapped to the tenant's words; the tenant's app signals (its login button);
the navigation fingerprint of each page an artifact starts on, keyed by path. Replay derives
`get_account_balance@0.2.0+tenant_b` with its own hash and logs the base hash; the base file is
never edited. Only whole UI-text values are renamed (locator names, labels, anchors, columns,
landmarks), never placeholders, URLs, codes or messages. Outcome messages stay in the base
language on every tenant: callers branch on the code, not the text.

**Producing an override for a real tenant.** (1) Replay the base artifacts on the tenant with no
override. Each stops at the first step that no longer fits and names it with the text it
expected, so the list of words to map comes from failures, not guesses. (2) Read the tenant's
words for those controls, either from that run's screenshots or by running discovery on the
tenant with the same goal and lining its steps up with the base steps. (3) Record the
fingerprint of each start page from a passing run. (4) Review and approve the file. Today step
(2) is manual (`tenant_b.yaml` was written from one look at the pages); the step-by-step diff
is a cut.

**Overrides are a policy change, not a copy edit.** The policy follows the renames, so "Sign In"
keeps the reversible rule "Log In" had and "Open a Share Account" stays irreversible; without
that, every renamed button would need approval. The flip side: a wrong mapping changes risk
classification. Mapping a base label onto a tenant control that moves money would let it run as
whatever the base control was. So an override gets the same review as `policies/parabank.yaml`,
by someone who checks each renamed control's risk, not only its spelling.

Tenant B is simulated in our own browser (`tenants/variants/tenant_b.yaml` renames page text).
Evidence, all live: the base artifact passes on the base app; on tenant B it fails at the first
step that no longer fits, naming it, with no false business answer; with the override it passes
with no warning, and `account_not_found` still works. The fingerprint is checked only for the
capability the caller asked for, on its own start page; preconditions such as sign-in pass
through other pages and are not checked. Later drift on a tenant shows as a fingerprint warning,
then as checkpoint failures.

## 5. Escalation & handoff

**Stuck** in discovery: the model calls `request_human`, an irreversible action needs approval,
or the loop stops itself (three actions changing nothing, step limit, time limit). In replay: an
irreversible step without approval, any failure after a commit, or an operator taking control.

**Routing.** With `--handoff`, the run writes an intervention to `runs/<id>/control.json`
(capability, step, intent, reason, redacted URL, masked screenshot), prints how to answer, and
keeps the browser open. Without a handoff, the same moment ends as `NeedsHuman`.

**Control.** `control.json` is a lease: owner (automation or human), holder, and an epoch that
rises on every change of hands. GuardedSurface checks it before every action, so `cua operator
take` stops automation before its next action, even mid-run. The person works in the same live
browser; capture logs each click and selection with its policy risk, never typed text.

**Handing back.** `approve <run> <id>`: automation does that one step only. `release`: the run
re-observes and verifies the step's checkpoint before going on (a person's irreversible click is
recorded as a commit). `decline`: the run ends. Answers name their request, so a yes cannot land
on another. If control changed hands unseen, the epoch differs and the run re-checks. Evidence:
`evidence/*-replay-open_new_account-8748` pauses at the irreversible step, is approved, completes.
The operator UI is a CLI on purpose; a web console would read and write the same lease.

## 6. Safety

- **Allowlist** (`policies/parabank.yaml`, unknown keys rejected): origins, paths (deny wins;
  admin console and APIs denied, matched after decoding so `%2561dmin` fails), action kinds,
  denied controls. A network guard aborts any denied page load, whoever caused it.
- **Risk**: known controls are classified; an unknown button is irreversible by default.
  Irreversible needs a person each time on a draft, or an approved artifact plus `--confirm`.
- **Data**: the model never sees a sensitive value (it reads `[REDACTED:account_id]` and types
  `{{account_id}}`). Logs are redacted by name and by shape (account and card numbers, SSNs,
  tokens, session ids). Screenshots mask secret fields and any element showing a sensitive
  value, and fail closed. Offline recordings are sanitized, then checked for every real value.

Limits: shape-based redaction cannot catch names or addresses, which must be declared (`--scrub`
for recordings). A person's click is checked and logged as it happens, not blocked beforehand;
denied page loads are still blocked. Dialogs are dismissed even while a person holds control.

## 7. Cuts

- **Agent-facing catalog** (approved artifacts as typed tools, e.g. MCP). The contract fits it.
- **Recorder-derived outcomes.** `account_not_found` is derived from the run;
  `invalid_credentials` and `funding_account_not_found` were added in review (`cua seal`). Next:
  derive "option missing" for select steps.
- **Tenant overrides beyond text**: a tool that diffs a tenant's discovered artifact against the
  base to propose the text map; per-tenant URL prefixes or extra steps; approving an effective
  artifact separately from its base.
- **Desktop surface, real operator console, replay-N stability scoring.** Seams are in place.
- **Offline coverage**: only `get_account_balance` has a pack; `session_expire` cannot trigger
  offline. Discovery still signs in with a small scripted login.
