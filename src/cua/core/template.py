"""`{{param}}` placeholders: how an artifact refers to a caller's input without containing it.

A recorded step says "click the link named {{account_id}}", not "click 13344". The value is bound
at replay time, so one artifact serves every account, and a sensitive value never sits on disk.

Kept deliberately tiny: no expressions, no filters, no escaping. A placeholder is a name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def placeholders(s: str) -> set[str]:
    """Names referenced in `s`."""
    return set(PLACEHOLDER.findall(s))


def render(s: str, params: Mapping[str, str]) -> str:
    """Replace every placeholder in `s`. A missing param is a KeyError: a step must never run
    with a half-filled value."""
    return PLACEHOLDER.sub(lambda m: params[m.group(1)], s)


def templatize(s: str, params: Mapping[str, str]) -> str:
    """The reverse of render: replace each param value found in `s` by its placeholder.

    Only whole-token occurrences are replaced, so the account id 133 does not turn "$1336.00"
    into "${{id}}6.00". Longest value first, so a value containing another is replaced whole."""
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if value:
            rx = re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
            s = rx.sub("{{" + name + "}}", s)
    return s
