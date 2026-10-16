"""The run log: the only way a run writes evidence to disk.

    runs/<run_id>/log.jsonl     one JSON event per line, every one passed through the redactor
    runs/<run_id>/NNN-name.png  screenshots, already masked by the surface
    runs/<run_id>/result.json   the final RunResult

There is no "write raw" method on purpose. Anything that reaches this class reaches disk
redacted, so a forgotten redact() call cannot leak a secret.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cua.core.redact import Redactor


def new_run_id(prefix: str) -> str:
    """Sortable and unique: 2026-10-15T10-15-00Z-discover-3f9a.

    Dashes keep every digit run short, so the redactor's account-number rule leaves it alone."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}-{prefix}-{secrets.token_hex(2)}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunLog:
    def __init__(self, root: str | Path, run_id: str, redactor: Redactor):
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._redact = redactor
        self._shots = 0

    def event(self, kind: str, **data: Any) -> None:
        record = self._redact.data({"t": utc_now(), "event": kind, **data})
        with (self.dir / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def screenshot(self, png: bytes | None, name: str) -> str | None:
        """Save a masked screenshot; returns its file name (relative to the run dir)."""
        if not png:
            return None
        self._shots += 1
        file = f"{self._shots:03d}-{name}.png"
        (self.dir / file).write_bytes(png)
        return file

    def result(self, result: BaseModel) -> None:
        body = self._redact.data(result)
        (self.dir / "result.json").write_text(
            json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
        )
