# cua

A computer-use automation system. An LLM discovers how to do a task in a legacy web app once.
The run becomes a typed, versioned capability artifact. Production replays that artifact
deterministically, with no LLM in the loop.

Target: the public [ParaBank](https://parabank.parasoft.com/parabank/) demo bank.

Work in progress. See `docs/PLAN.md` for milestones.

## Setup

```
uv sync
uv run playwright install chromium
```

Create `.env` in the repo root (never committed):

```
PARABANK_USERNAME=<your ParaBank test user>
PARABANK_PASSWORD=<its password>
ANTHROPIC_API_KEY=<key, only needed for discovery>
ANTHROPIC_WORKSPACE_ID=<only if the key is not scoped to one workspace>
```

The public ParaBank database resets from time to time. If login fails, register the test user
again on the site (fake data only) and update `.env`. Account ids change after a reset.

## Milestone checks

```
uv run python scripts/m1_check.py          # M1: snapshot of login page + accounts overview
uv run pytest tests/core/test_policy.py    # M2: admin.htm and unapproved irreversible clicks blocked
uv run pytest                              # offline unit tests
```

M3, discovery on the live site (one model run, writes a draft artifact and its evidence):

```
uv run cua discover --name get_account_balance --login     --goal "Find the balance and the available amount of the given account"     --input account_id=<an account id of the test user>     --output balance --output available     --runs-dir evidence
```

The model never sees sensitive values: `account_id` shows on screen as `[REDACTED:account_id]`
and the model types inputs as `{{account_id}}`. The draft lands in
`artifacts/get_account_balance@0.1.0.json`, the redacted log and screenshots in `evidence/<run>/`.
Irreversible clicks (Transfer, Open New Account, ...) stop and ask on the terminal first.

M4, replay with no model. Signs in by replaying `ensure_session` (credentials from `.env`):

```
A=artifacts/get_account_balance@0.2.0.json
uv run cua replay $A --input account_id=<id> --runs-dir evidence                       # success
uv run cua replay $A --input account_id=12345 --runs-dir evidence                      # account_not_found
uv run cua replay $A --input account_id=<id> --inject http500 --runs-dir evidence      # retried
uv run cua replay $A --input account_id=<id> --inject session_expire --runs-dir evidence  # re-login
```

Exit codes: 0 success, 3 business outcome, 4 needs a human, 1 failure. Faults run in our own
browser only (`slow[:ms]`, `http500[:n]`, `session_expire`, `dialog`); the app sees no extra load.

More M4 evidence: `--inject http500:3` (retries used up, ends as `app_error` with a screenshot),
`--inject dialog` and `--inject slow`. A wrong password ends as the business outcome
`invalid_credentials` (exit 3), not as an app error:

```
uv run cua replay artifacts/ensure_session@0.2.0.json --input username=nobody --input password=x --runs-dir evidence
```

Rules after an irreversible step (money moved, an account opened): the run never signs in again,
rewinds, retries a page load or clicks again. Anything that goes wrong after that point ends as
`needs_human`, naming the step that committed.

Review status lives in each artifact: `draft`, `approved` or `deprecated` (refused by replay,
with a note saying why). It is not part of `content_hash`, and every result records which
artifact ran, its status and its hash. After a reviewer edits a draft (for example, adding an
outcome), `uv run cua seal <artifact>` recomputes the hash; approved artifacts are never re-sealed.

M5, human handoff. A draft irreversible capability pauses at its irreversible step and waits for
a person, with the browser kept open:

```
uv run cua replay artifacts/open_new_account@0.1.0.json \
    --input account_type=SAVINGS --input funding_account_id=<id> \
    --headed --handoff --runs-dir evidence
```

The run prints what it needs (capability, step, reason, page, masked screenshot). In another
terminal:

```
uv run cua operator list --runs-dir evidence
uv run cua operator approve <run> --by <name>     # automation does this one step
uv run cua operator take <run> --by <name>        # or do it yourself in the window...
uv run cua operator release <run> --by <name>     # ...then hand back; the run re-checks the page
uv run cua operator decline <run> --by <name>     # or say no
```

`<run>` is the run directory or any unique part of its id. Control lives in
`<run>/control.json` (owner and epoch). Automation checks it before every action, so a `take` at
any moment stops the run before its next action. While a person holds control, their clicks and
selections are logged with their risk (typed text is never logged). Discovery takes `--handoff`
too, to ask for approvals this way instead of on the terminal.
