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
