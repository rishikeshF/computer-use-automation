"""Offline mode end to end: replay from a recording through the real WebSurface.launch, with no
route handler and no network. Anything the recording lacks is aborted, never fetched."""

import json
from decimal import Decimal
from pathlib import Path

from cua.core.result import BusinessOutcome, Success
from cua.core.runlog import RunLog
from cua.replay.engine import ReplayOptions, replay
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface
from tests.fakebank import BAD, BALANCE, BASE, CREDS, GOOD, LOGIN, POLICY, SESSION, Bank, overview


def entry(url: str, html: str) -> dict[str, object]:
    return {
        "startedDateTime": "2026-10-16T00:00:00.000Z",
        "time": 0,
        "request": {
            "method": "GET",
            "url": url,
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": [],
            "queryString": [],
            "headersSize": -1,
            "bodySize": 0,
        },
        "response": {
            "status": 200,
            "statusText": "OK",
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": [{"name": "Content-Type", "value": "text/html"}],
            "content": {"size": -1, "mimeType": "text/html", "text": html},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": 0, "receive": 0},
    }


def recording(tmp_path: Path) -> Path:
    """What a recorded sign-in plus overview looks like: two pages."""
    har = {
        "log": {
            "version": "1.2",
            "creator": {"name": "test", "version": "1"},
            "entries": [
                entry(f"{BASE}/index.htm", LOGIN),
                entry(f"{BASE}/overview.htm", overview(Bank())),
            ],
        }
    }
    path = tmp_path / "bank.har"
    path.write_text(json.dumps(har), encoding="utf-8")
    return path


async def run_offline(tmp_path: Path, account: str) -> tuple[object, WebSurface]:
    async with WebSurface.launch(
        allowed_url=POLICY.url_allowed, har=recording(tmp_path), polite_delay_ms=0
    ) as web:
        surface = GuardedSurface(web, POLICY, sensitive={"account_id": account, **CREDS})
        log = RunLog(tmp_path, "run", surface.redact, base=tmp_path)

        async def sign_in():  # type: ignore[no-untyped-def]
            return await replay(SESSION, CREDS, surface, log, ReplayOptions(base_url=BASE))

        opts = ReplayOptions(base_url=BASE, preconditions={"ensure_session": sign_in})
        result = await replay(BALANCE, {"account_id": account}, surface, log, opts)
        # A page that was never recorded is aborted, not fetched from the network.
        failed = await web.page.evaluate(
            "fetch('/bank/transfer.htm').then(() => 'fetched', () => 'aborted')"
        )
        return result, failed


async def test_replay_runs_from_the_recording(tmp_path: Path) -> None:
    result, unrecorded = await run_offline(tmp_path, GOOD)
    assert isinstance(result, Success), result
    assert result.outputs["balance"] == Decimal("515.50")
    assert unrecorded == "aborted"


async def test_business_outcomes_work_offline_too(tmp_path: Path) -> None:
    result, _ = await run_offline(tmp_path, BAD)
    assert isinstance(result, BusinessOutcome) and result.code == "account_not_found"
