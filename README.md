# cua

A computer-use automation system. An LLM discovers how to do a task in a legacy web app once.
The run becomes a typed, versioned capability artifact. Production replays that artifact
deterministically, with no LLM in the loop. When it cannot go on safely, a person takes over the
same live browser and hands it back.

Target: the public [ParaBank](https://parabank.parasoft.com/parabank/) demo bank (built by
Parasoft for test automation). Design write-up: [REPORT.md](REPORT.md).

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run playwright install chromium
```

That is all the offline demo needs. For live runs, create `.env` in the repo root (never
committed):

```
PARABANK_USERNAME=<your ParaBank test user>
PARABANK_PASSWORD=<its password>
ANTHROPIC_API_KEY=<key, only needed for discovery>
ANTHROPIC_WORKSPACE_ID=<only if the key is not scoped to one workspace>
```

Register the test user on the ParaBank site with fake data. The public database resets from time
to time; then sign-in returns `invalid_credentials` and you register again. Account ids change
after a reset.

## Demo in five minutes, offline

No network, no `.env`, no model. `offline/get_account_balance.har` is a recorded live run with
every sensitive value replaced by a stand-in; `offline/get_account_balance.json` says which.

```
A=artifacts/get_account_balance@0.2.0.json
P=offline/get_account_balance.json
uv run cua replay $A --offline $P                            # success: balance 515.50
uv run cua replay $A --offline $P --input account_id=12345   # account_not_found (exit 3)
uv run cua replay $A --offline $P --inject http500           # page load retried, then success
uv run cua replay $A --offline $P --inject http500:3         # retries used up: app_error (exit 1)
uv run cua replay $A --offline $P --inject dialog            # alert dismissed and noted
uv run cua replay $A --offline $P --variant tenants/variants/tenant_b.yaml     --tenant tenants/tenant_b.yaml                           # another tenant: see Tenants
uv run pytest                                                # 167 tests, all offline
```

Each run prints its result and writes a redacted log and screenshots to `runs/<run id>/`.
Exit codes: 0 success, 3 business outcome, 4 needs a person, 1 failure.

## Demo path, live

**1. Discover.** The model works out the task once, under policy (one model run):

```
uv run cua discover --name get_account_balance --login \
    --goal "Find the balance and the available amount of the given account" \
    --input account_id=<an account id of the test user> \
    --output balance --output available --runs-dir evidence
```

The model never sees sensitive values: `account_id` shows on screen as `[REDACTED:account_id]`
and the model types inputs as `{{account_id}}`. The draft artifact lands in `artifacts/`, the
redacted log and screenshots in `evidence/<run>/`.

**2. Replay** with no model. Signs in first by replaying `ensure_session` (credentials from
`.env`):

```
A=artifacts/get_account_balance@0.2.0.json
uv run cua replay $A --input account_id=<id> --runs-dir evidence                          # success
uv run cua replay $A --input account_id=12345 --runs-dir evidence                         # account_not_found
uv run cua replay $A --input account_id=<id> --inject session_expire --runs-dir evidence  # signs in again
uv run cua replay artifacts/ensure_session@0.2.0.json \
    --input username=nobody --input password=x --runs-dir evidence                        # invalid_credentials
```

Faults are injected in our own browser only (`slow[:ms]`, `http500[:n]`, `session_expire`,
`dialog`); the app sees no extra load.

**3. Hand off to a person.** `open_new_account` has an irreversible step. Unless the artifact is
approved and the caller passes `--confirm`, that step pauses for a person, with the browser kept
open:

```
uv run cua replay artifacts/open_new_account@0.2.0.json \
    --input account_type=SAVINGS --input funding_account_id=<id> \
    --headed --handoff --runs-dir evidence
```

The run prints the request (capability, step, reason, page, masked screenshot) and its id. In
another terminal:

```
uv run cua operator list --runs-dir evidence
uv run cua operator approve <run> <request id> --by <name>   # automation does this one step
uv run cua operator take <run> --by <name>                   # or do it yourself in the window,
uv run cua operator release <run> --by <name>                # then hand back; the run re-checks
uv run cua operator decline <run> <request id> --by <name>   # or say no
```

`<run>` is the run directory or any unique part of its id. Approve and decline name the request
they answer, so a yes given while looking at one request can never land on another.

Discovery can ask for its approvals the same way: add `--handoff` to `cua discover`.

## Tenants

Many institutions run the same vendor product with their own words on it. An artifact is
recorded once; each tenant gets one override for the whole app (`tenants/<tenant>.yaml`): base
UI text mapped to the tenant's text, how the tenant shows a login form, and the navigation
fingerprint of each page an artifact starts on. Replay derives `<name>@<version>+<tenant>` with its own hash and logs the base hash
it came from. The base artifact is never edited, and the policy follows the renames ("Sign In"
keeps the rule "Log In" had).

Tenant B is simulated in our own browser: `--variant tenants/variants/tenant_b.yaml` renames
page text ("Accounts Overview" becomes "Account Summary", "Log In" becomes "Sign In", ...).

```
A=artifacts/get_account_balance@0.2.0.json
V="--variant tenants/variants/tenant_b.yaml"
uv run cua replay $A --input account_id=<id> --runs-dir evidence                          # base app: success
uv run cua replay $A --input account_id=<id> $V --runs-dir evidence                       # tenant B, no override: Failure naming the step
uv run cua replay $A --input account_id=<id> $V --tenant tenants/tenant_b.yaml --runs-dir evidence   # tenant B + override: success
```

## Rules worth knowing

- Every action, in discovery, replay and handoff, goes through `policies/parabank.yaml` first.
  The admin page is denied; buttons that move money or create records are irreversible.
- After an irreversible step has run, a replay never signs in again, rewinds, retries or clicks
  again. Anything that goes wrong after that point ends as `needs_human`.
- Artifacts carry a review status: `draft`, `approved` or `deprecated` (refused by replay). It
  is not part of `content_hash`; every result records the artifact id, status and hash. After a
  reviewer edits a draft, `uv run cua seal <artifact>` recomputes the hash.
- Secrets never reach disk: logs are redacted, screenshots masked, artifacts hold `{{param}}`
  placeholders, and offline recordings are sanitized and checked before they are written.

## Recording a new offline pack

From a live run, with a working `.env`:

```
uv run cua replay artifacts/get_account_balance@0.2.0.json --input account_id=<id> \
    --record-har offline/get_account_balance.json --scrub <surname shown on the page>
```

The raw recording goes to a temp directory and is deleted. Only the sanitized copy is written,
and only if none of the real values (inputs, credentials, `--scrub` text) is left in it.

## Layout

```
src/cua/
  core/       artifact schema, results, policy, redaction, run log, offline packs, tenants
  surface/    the only code that touches Playwright: snapshot, locators, faults, capture, variants
  discover/   the model loop and the recorder that turns its trace into an artifact
  replay/     the deterministic engine and the app-level detectors
  handoff/    the control lease (control.json) and the run's side of a handoff
  llm/        the model client, used by discovery only
  cli.py      discover, replay, operator, seal
artifacts/    capability artifacts, versioned
evidence/     committed demo runs (discovery, replays, failures, handoff)
offline/      recorded, sanitized app traffic for offline replay
tenants/      tenant overrides, and simulated tenant variants for demos
policies/     the safety policy
```
