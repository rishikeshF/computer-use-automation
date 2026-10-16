"""Offline mode: a recorded run that is safe to commit, and replays with no network.

Recording a live replay (`cua replay --record-har`) captures everything the browser sent and got
back: the login form post with the username and password, the session cookie, account numbers in
URLs and pages. None of that may reach the repo. sanitize() rewrites the recording so it can:

  - each sensitive value becomes a stand-in, everywhere it appears (URLs, form posts, pages,
    JSON), the same way every time. Requests still match their responses offline, and the
    offline run types the stand-ins ("demo-password") instead of the real values.
  - session ids become a constant; Content-Length headers are dropped (bodies changed length).
  - then it checks: if any real value is still in the text, nothing is written.

The pack (offline/<name>.json) says which stand-ins to use, so a fresh clone with no .env can run.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

from pydantic import BaseModel, ConfigDict

_SESSION_ID = re.compile(r"(jsessionid=)[A-Za-z0-9._-]+", re.IGNORECASE)
_DROP_HEADERS = {"content-length"}


class OfflinePack(BaseModel):
    """offline/<name>.json: what an offline replay needs besides the recording itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    har: str  # the recording, next to this file
    capability: str  # the artifact it was recorded with
    base_url: str
    recorded_at: str
    inputs: dict[str, str]  # stand-ins for the caller's inputs (account_id)
    preconditions: dict[str, dict[str, str]]  # stand-ins for their inputs (credentials)
    note: str = ""


def stand_ins(sensitive: Mapping[str, str], taken: str) -> dict[str, str]:
    """name -> stand-in. Digits stay digits of the same length (an account id still looks like
    one, so patterns and page layout hold), chosen so they appear nowhere in `taken`."""
    out: dict[str, str] = {}
    for name, value in sensitive.items():
        if value.isdigit():
            candidate = int("1" + "0" * (len(value) - 1))
            while str(candidate) in taken or str(candidate) in out.values():
                candidate += 7919  # a prime step: walks the space without repeating soon
            out[name] = str(candidate)
        else:
            out[name] = "demo-" + name.replace("_", "-")
    return out


def sanitize(har: dict[str, Any], replace: Mapping[str, str]) -> dict[str, Any]:
    """A copy of `har` with every real value (key) replaced by its stand-in (value), also where
    it appears URL-encoded (a password with "@" is posted as "%40")."""
    forms = {enc: fake for real, fake in replace.items() for enc in _forms(real)}
    rules = [
        (re.compile(rf"(?<![A-Za-z0-9]){re.escape(real)}(?![A-Za-z0-9])"), fake)
        for real, fake in sorted(forms.items(), key=lambda kv: -len(kv[0]))
        if real
    ]

    def text(s: str) -> str:
        for rx, fake in rules:
            s = rx.sub(fake, s)
        return _SESSION_ID.sub(r"\1OFFLINE", s)

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return text(node)
        if isinstance(node, list):
            return [
                walk(v)
                for v in node
                if not (isinstance(v, dict) and str(v.get("name", "")).lower() in _DROP_HEADERS)
            ]
        if isinstance(node, dict):
            if node.get("encoding") == "base64":  # images and fonts: bytes, not text
                return {k: v if k == "text" else walk(v) for k, v in node.items()}
            return {k: walk(v) for k, v in node.items()}
        return node

    return walk(har)  # type: ignore[no-any-return]


def leaks(har: dict[str, Any], secrets: Iterable[str]) -> list[str]:
    """Real values still in `har`: in its text, or in the decoded bytes of a binary body.
    Must be empty before it is written."""
    texts: list[str] = []
    blobs: list[bytes] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("encoding") == "base64" and isinstance(node.get("text"), str):
                blobs.append(base64.b64decode(node["text"], validate=False))
                node = {k: v for k, v in node.items() if k != "text"}
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            texts.append(node)

    walk(har)
    body = "\n".join(texts)
    return [
        s
        for s in secrets
        if s and (any(f in body for f in _forms(s)) or any(s.encode() in b for b in blobs))
    ]


def _forms(value: str) -> set[str]:
    """A value as it may appear in a recording: as is, and URL-encoded both ways."""
    return {value, quote(value, safe=""), quote_plus(value)}
