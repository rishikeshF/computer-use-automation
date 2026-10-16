# REPORT

Target: the public ParaBank demo bank, a server-rendered JSP app with table layouts, no test ids
and a login form that labels nothing. Three capabilities: `ensure_session` (sign in),
`get_account_balance` (read-only, can answer `account_not_found`) and `open_new_account`
(irreversible, can answer `funding_account_not_found`). Evidence for every claim below is in
`evidence/`; the offline demo is in the README.

## 1. Architecture

One Python process, asyncio, plain files. No services, queues or database: the brief rewards a
small correct system, and every seam that would need to scale (the surface, the artifact store,
the operator channel) is an interface with one implementation.

```
discover (model loop) --trace--> recorder --artifact JSON--> replay engine --result--> caller
        \                                                         |
         +--------------- GuardedSurface (policy, masks, lease) --+--- Surface (Playwright)
                                                                  |
                                     handoff: control.json <--> cua operator (a person)
```

- **Surface** (`observe`, `resolve`, `act`) is the only code that touches Playwright. Perception
  is a frame-aware snapshot of what an operator sees: role, name, inferred label, table row and
  column, box. The model acts on snapshot refs and never writes a selector.
- **GuardedSurface** wraps it, and is all that discovery, replay and handoff get: every action
  passes the policy and the control lease first, every screenshot is masked.
- **Discovery** is a boring observe, decide, act loop with Claude Sonnet via tool calling. The
  model does the one thing only a model can do (pick the next control on an unseen page);
  policy, approval, redaction, stop conditions and recording are plain code.
- **Replay** is a deterministic state machine over the artifact. It cannot import the model:
  `tests/test_boundaries.py` walks the import graph and fails if it ever does.

Trade-off: accessibility-style perception, not screenshot coordinates. It reads frames and tables
directly and survives restyling; it is the same model a desktop accessibility API gives, so it is
not a web-only bet. The cost is that canvas-drawn UIs would need the BBox strategy (section 4).

## 2. Artifact schema

`artifacts/<name>@<semver>.json`, a Pydantic model. The contract a calling agent sees:

- `inputs`: typed, required, `sensitive`, optional regex. Sensitive ones are masked everywhere.
- `outputs`: name, type (`decimal` turns "$515.50" into 515.50), unit, and the step that reads it.
- `outcomes`: the business answers it can give instead of the happy path, each with a message.
- `risk` (`read_only`, `reversible`, `irreversible`) and `status` (`draft`, `approved`,
  `deprecated`).

What replay needs, per step: a plain-English `intent` (written by the model while acting, read
by reviewers), an `action`, a `target` LocatorChain, a `value` (literal or `{{param}}`), and
`pre` and `post` checkpoints (URL pattern with params, a landmark heading, an element).

Decisions worth defending:

- **Locators describe, they do not point.** A chain is ordered fallbacks: role and name, inferred
  label, table cell by row and column, text anchor ("combobox below 'Please choose an existing
  account'"), then CSS (flagged fragile). The recorder keeps a strategy only if it matches
  exactly the recorded control on the recorded page, keeps text anchors only from the text that
  labels the control (nearest thing on that side, within 40 px), and drops fallbacks that ignore
  an input (on a page with more rows, "the first balance cell" would quietly read the wrong
  account).
- **Business outcomes are declared, not inferred at runtime.** `account_not_found` says: on the
  overview, once the column headers and at least one data row are present, the link named
  `{{account_id}}` is absent. The `present` guard is what keeps a renamed column from becoming a
  false "not found": it fails too, and the run is a Failure a person looks at.
- **No caller value is ever stored.** Values become `{{param}}`, in targets, URLs and checkpoints.
  The recorder refuses to write an artifact that still contains a sensitive value.
- **`content_hash` covers behavior only** (not id, status or provenance). Approving or deprecating
  does not change it; editing a step does, and replay refuses an artifact whose hash no longer
  matches. Every result records which artifact ran, its status and its hash.
- `app.app_version_fingerprint` hashes the start page's navigation; `overrides` is reserved for
  per-tenant patches (section 4).

## 3. Determinism & error handling

Same inputs, same steps, verified at every step: wait for `pre`, check the declared outcomes,
resolve the target (first strategy with exactly one match; ties are reported, never guessed),
act through policy, wait for `post`. Waiting is polling of real state with a step timeout, never
a fixed sleep.

Every run ends in exactly one result, and only bugs raise:

| Result | Meaning | Examples |
|---|---|---|
| `Success` | done; typed outputs | balance 515.50 |
| `BusinessOutcome` | the app answered "no"; the caller branches on `code` | `account_not_found`, `invalid_credentials`, `funding_account_not_found` |
| `NeedsHuman` | paused on purpose | unapproved irreversible step; anything after a commit |
| `Failure` | the automation could not do its job; `kind`, step, expected, observed, screenshot | `locator_not_found`, `checkpoint_mismatch`, `app_error`, `action_failed`, `policy_violation` |

Recoverable conditions never change the status; they are listed in `evidence.recoveries`:

- **Session expired** (login form present): replay `ensure_session`, resume from the last page load.
- **Failed or 500 page load**: retried with backoff (page loads only; clicks are never retried).
- **Unexpected dialog**: dismissed and noted.
- **Slow load**: absorbed by the waits.

App-level detectors run on every look, in an order that matters: on ParaBank an expired session
shows the login form and "internal error" on the same page, and a wrong password shows an
"Error!" heading. Declared outcomes are checked before the app-error detector, or the bank's own
answers would look like server faults.

**Irreversible steps change the rules.** The step counts as committed once policy allows the
action and before it runs, so a click that lands and then errors is still "it may have
happened". After that the run never signs in again, rewinds, retries or clicks again; every
failure becomes `NeedsHuman` naming the committed step. Tests count accounts opened on a fake
server and prove an account is never opened twice.

Drift (secondary, since bank UIs change slowly): fallbacks absorb small changes and evidence
records which strategy fired; a changed navigation fingerprint is a warning; a reshaped page
fails a checkpoint or a guard and is reported as a Failure, never as a wrong answer.

Faults are injected in our own browser (Playwright routes): `slow`, `http500[:n]`,
`session_expire`, `dialog`. The shared demo never sees extra load.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The seam is the Surface protocol and the Node it returns (role, name, label, table
position, frame path, box). Artifacts only contain Nodes' descriptions, never markup. Matching
(role and name, label, table cell, text anchor) is plain Python over Nodes, so a new surface gets
it for free:

- A legacy web app is this one: frames, tables and unlabeled inputs are handled already.
- A desktop app would be a `UIASurface` producing the same Nodes from the Windows UI Automation
  tree (or the macOS accessibility tree), with `act` via the platform's input APIs. Replay,
  policy, handoff and the schema do not change.
- A surface with no element tree at all (a remote desktop image) would use the `BBox` strategy
  with screenshot matching; it exists in the schema and is refused on web, where a tree is
  always available.

**Tenants.** Hundreds of tenants run the same vendor product, configured differently. The design:
one base artifact per vendor capability, keyed by `app.vendor` and version, plus small
per-tenant `overrides` that patch only what differs (a label, a landmark, a URL prefix,
`AppSignals` like the error heading). The base keeps its hash; the tenant's effective artifact
is base plus patch, hashed and approved on its own. Drift is detected three ways: the
navigation fingerprint (cheap, every run), checkpoint and guard failures (precise, per step), and
which fallback strategy fired (trend over runs). A tenant that drifts degrades to a Failure that
names the step, and a re-discovery on that tenant produces a candidate override to review. Built:
the fingerprint, `base_url` as a parameter, `AppSignals` per app, the `overrides` field. Not
built: applying overrides (section 7).

## 5. Escalation & handoff

**Detecting stuck.**
- In discovery: the model calls `request_human`, an irreversible action needs approval, or the
  loop stops itself (three actions in a row that change nothing, a step limit, a time limit).
- In replay: an irreversible step without approval, anything going wrong after a commit, or an
  operator taking control unasked.

**Routing.** With `--handoff`, the run writes an intervention into `runs/<id>/control.json`:
capability, step, intent, reason, redacted URL and a masked screenshot. It prints how to answer
and keeps the browser open. Without a handoff, the same moment ends the run as `NeedsHuman`.

**Taking control.** `control.json` is a lease: `owner` (automation or human), `holder`, and an
`epoch` that rises on every change of hands. GuardedSurface checks it before every action, so
`cua operator take` stops automation before its next action, even mid-run. The person works in the
same live, headed browser. Capture listeners log each click and selection with its policy risk;
typed text is never read.

**Handing back.**
- `approve <run> <request id>`: automation does that one step, and only that one.
- `release`: the run re-observes and verifies the step's checkpoint before it continues. If the
  person did the irreversible step (capture saw it), it is recorded as committed.
- `decline`: the run ends without doing the step.

Approve and decline must name the request they answer, so a yes cannot land on a different
request. If control changed hands while automation was not looking, the epoch differs and the
run re-checks rather than carrying on blind. Evidence:
`evidence/*-replay-open_new_account-2677` pauses at the irreversible step, is approved, and
completes.

The operator UI is a CLI on purpose (the brief allows a mock). A web console would read and
write the same file, or the same record in a database.

## 6. Safety

- **Allowlist** (`policies/parabank.yaml`, validated, unknown keys rejected): origins, paths
  (deny wins; the admin console and the APIs are denied), action kinds, denied controls.
  Paths are matched after decoding and normalizing, so `%2561dmin` or `x/../admin.htm` do not
  slip through.
- **Network guard**: aborts any page load the policy denies, whoever caused it (a misjudged link,
  a redirect, a person in the handoff window).
- **Risk**: known buttons are classified (Transfer, Open New Account, ...). An unknown button is
  irreversible by default: in a bank, asking once costs less than an unwanted transfer.
  Irreversible needs a person each time for a draft, or an approved artifact plus the caller's
  explicit `confirm`.
- **Data**: the model never sees a sensitive value (it reads `[REDACTED:account_id]`, types
  `{{account_id}}`). Logs pass through a redactor that knows the run's secrets by name and also
  masks shapes (account and card numbers, SSNs, tokens, session ids). Screenshots mask secret
  fields and any element showing a sensitive value, and fail closed. Artifacts hold placeholders
  only. Offline recordings are sanitized, then checked for every real value before they are
  written.

Limits, stated plainly:
- Shape-based redaction is a safety net, not a guarantee: a name or an address has no shape, so
  it must be declared.
- A person's click in the handoff window is checked as it happens and logged, not blocked
  beforehand; denied page loads are still blocked by the network guard.
- Dialogs are dismissed automatically, even while a person holds control.
- The redactor replaces every match of a secret, so an operator whose name equals the test
  username appears as `[REDACTED:username]`.

## 7. Cuts

Left out on purpose, and what is next:

- **Applying tenant overrides.** The field and the design exist. Next: a second ParaBank variant
  via route interception (renamed labels), with one base artifact and a small override.
- **An agent-facing catalog** (approved artifacts as typed tools, e.g. an MCP server). The
  artifact contract is already shaped for it.
- **The recorder deriving more outcomes.** `account_not_found` is derived from the run;
  `invalid_credentials` and `funding_account_not_found` were added in review (and re-sealed with
  `cua seal`). Next: derive "option missing from a list" for select steps, as for rows.
- **Desktop surface, real operator console, replay-N stability scoring.** Seams are in place.
- **Offline coverage.** Only `get_account_balance` has a recorded pack; `session_expire` cannot
  trigger offline (the recording answers regardless of cookies). Recording `open_new_account`
  means opening a real (demo) account, so it waits for a person to approve one.
- Discovery still signs in with a small scripted login; replay uses the `ensure_session` artifact.
