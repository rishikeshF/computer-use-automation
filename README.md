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
```

## Milestone checks

```
uv run python scripts/m1_check.py          # M1: snapshot of login page + accounts overview
uv run pytest                              # offline unit tests
```
