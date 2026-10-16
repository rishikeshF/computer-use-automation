"""Redaction: the one gate every log line, artifact and evidence file passes through.

Two layers:
  1. Known secrets. Values we were handed and know are sensitive (credentials, sensitive input
     params) are replaced by name: "[REDACTED:account_id]". Exact, so nothing slips through.
  2. Shapes. Things that look sensitive even when nobody told us: session ids in URLs, tokens,
     API keys, SSNs, emails, and long digit runs (account and card numbers, last 4 kept).

Layer 2 is a safety net, not a guarantee: a name or an address has no reliable shape. That is
why sensitive inputs are declared (policy + artifact) and handled by layer 1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from cua.core.url import SESSION_IN_PATH

# Keys whose values are always secret, in "key=value" text and in structured data.
_SECRET_KEY = r"password|passwd|pwd|secret|token|access_token|api[_-]?key|ssn|jsessionid"
# Header-style keys whose value runs to the end of the line ("Authorization: Bearer x.y.z",
# "Cookie: a=1; b=2"). Redacting only the first word would leave the token in clear.
_SECRET_HEADER = r"authorization|proxy-authorization|cookie|set-cookie"
_SECRET_KEY_RX = re.compile(rf"^(?:{_SECRET_KEY}|{_SECRET_HEADER})$", re.IGNORECASE)

_SHAPES: list[tuple[re.Pattern[str], str]] = [
    (SESSION_IN_PATH, ";jsessionid=[REDACTED]"),
    (re.compile(rf"\b({_SECRET_HEADER})(\s*:\s*)[^\r\n]*", re.IGNORECASE), r"\1\2[REDACTED]"),
    (
        re.compile(rf"\b({_SECRET_KEY})(\s*[=:]\s*)(\"?)[^\s\"&;,]+", re.IGNORECASE),
        r"\1\2\3[REDACTED]",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED:api_key]"),
    # Every domain label holds a letter and the TLD is letters only, so a versioned file name
    # like "get_account_balance@0.1.0.json" is not an email.
    (re.compile(r"\b[\w.+-]+@(?:[\w-]*[A-Za-z][\w-]*\.)+[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
]
# 5 to 17 digits standing alone: account or card number. Not part of an amount like $12345.67,
# and not part of a hex string such as a content hash (no hex letter on either side).
_LONG_NUMBER = re.compile(r"(?<![\da-fA-F$.,])\d{5,17}(?![\da-fA-F]|[.,]\d)")


def _mask_number(m: re.Match[str]) -> str:
    digits = m.group(0)
    return "*" * (len(digits) - 4) + digits[-4:]


class Redactor:
    def __init__(self, secrets: Mapping[str, str] | None = None):
        # Longest value first, so a secret that contains another is replaced whole.
        items = sorted((secrets or {}).items(), key=lambda kv: -len(kv[1]))
        self._secrets = [
            (re.compile(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])"), f"[REDACTED:{k}]")
            for k, v in items
            if v
        ]

    def text(self, s: str) -> str:
        for rx, label in self._secrets:
            s = rx.sub(label, s)
        for rx, repl in _SHAPES:
            s = rx.sub(repl, s)
        return _LONG_NUMBER.sub(_mask_number, s)

    def data(self, obj: Any) -> Any:
        """Redact a JSON-like structure (or a pydantic model) recursively, returning a copy."""
        if isinstance(obj, BaseModel):
            obj = obj.model_dump(mode="json")
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, Mapping):
            return {
                k: "[REDACTED]" if isinstance(k, str) and _SECRET_KEY_RX.match(k) else self.data(v)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [self.data(v) for v in obj]
        return obj

    __call__ = text


def redact(value: Any, secrets: Mapping[str, str] | None = None) -> Any:
    """One-off redaction. Prefer a long-lived Redactor when the same secrets are reused."""
    r = Redactor(secrets)
    return r.text(value) if isinstance(value, str) else r.data(value)
