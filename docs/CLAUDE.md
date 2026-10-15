# CLAUDE.md

## What this repo is
A computer-use automation system (interface.ai take-home, built to production standard as a portfolio piece).
An LLM discovers how to do a task in a legacy web app once, the run is compiled into a typed, versioned
capability artifact, and production replays that artifact deterministically with no LLM in the loop.

Through-line: the model discovers, the artifact is the capability, deterministic replay is how agents call it.

## Target
ParaBank public demo: https://parabank.parasoft.com/parabank/
- Built by Parasoft for test automation. Use only throwaway accounts with fake data. Never real PII.
- NEVER touch the public admin page (admin.htm). Other people share this instance.
- Be polite: no parallel runs, no tight loops, keep replay-N small (N <= 10), add small delays.
- The public DB resets sometimes. A missing test user is a real business outcome, not a bug.

No Docker. Fault injection is client-side only (Playwright route interception on our own browser).

## Stack (decided)
- Python 3.12, uv, Pydantic v2, Playwright (async), Typer CLI, pytest
- LLM: Anthropic Claude (Sonnet tier) behind a small `LLMClient` protocol; OpenAI adapter optional
- Storage: plain files (artifacts as JSON, runs as JSONL + screenshots). No DB, no queues, no services.

## Non-negotiable rules
1. Replay path imports nothing from `llm/`. Enforce with a test.
2. Every action goes through `policy.check()` before execution, in discovery, replay and human handoff.
3. Secrets and sensitive params never reach disk. Logs pass through `redact()`; screenshots use Playwright `mask=`.
4. `Surface` is the only module that touches Playwright. Artifacts never contain raw CSS/XPath as the primary locator.
5. Business outcomes are results, not exceptions. Only hard failures raise.
6. Keep it small. Every file should be defensible in an interview. No framework name-dropping.

## Conventions
- Type everything. `mypy --strict` on `core/`.
- Tests live next to the concept they test; replay engine and policy get the most tests.
- Commit per milestone. Do not start the next milestone until the current one's acceptance check passes and Rishi has reviewed.
- Writing in docs: short, plain, no em-dashes, simple, easy to understand, comments wherever possible.

See PLAN.md for milestones.
