"""URL normalization shared by policy (what may we touch) and redaction (what may we write down).

Legacy Java apps put the session id in the path (`index.htm;jsessionid=8F58...`). That is a
session token: it must never reach a log, and it must never stop a path rule from matching.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from cua.core.template import render

SESSION_IN_PATH = re.compile(r";jsessionid=[^?#/]*", re.IGNORECASE)


def strip_session(url: str) -> str:
    return SESSION_IN_PATH.sub("", url)


def origin(url: str) -> str | None:
    """`scheme://host[:port]` in lower case, or None for non-web URLs (javascript:, data:, ...)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}".lower()


def canonical_path(url: str) -> str:
    """The path a rule should be matched against, with every disguise removed.

    Lower-cased, percent-decoded until stable (so `%2561dmin` cannot hide `admin`), session id
    stripped, and `..` segments collapsed (so `/parabank/x/../admin.htm` is seen as admin.htm).
    """
    path = urlsplit(url).path or "/"
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    path = posixpath.normpath(strip_session(path).replace("\\", "/"))
    return "/" if path in (".", "") else path.lower()


def url_pattern(url: str, params: Mapping[str, str]) -> str:
    """A checkpoint pattern for a page: canonical path plus sorted query, where a query value equal
    to a param becomes its placeholder and any other value becomes `*`.

        https://x/parabank/activity.htm;jsessionid=AB?id=13344
        -> /parabank/activity.htm?id={{account_id}}

    Values that are not params are wildcarded because they are run data (a new account id, a
    timestamp) and would make the checkpoint fail on every other run."""
    by_value = {v: k for k, v in params.items() if v}
    query = sorted(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    parts = [f"{k}={'{{' + by_value[v] + '}}' if v in by_value else '*'}" for k, v in query]
    path = canonical_path(url)
    return f"{path}?{'&'.join(parts)}" if parts else path


def url_matches(url: str, pattern: str, params: Mapping[str, str]) -> bool:
    """Does a live URL satisfy a recorded pattern, with params bound?"""
    want_path, _, want_query = pattern.partition("?")
    if canonical_path(url) != want_path:
        return False
    have = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    for item in filter(None, want_query.split("&")):
        key, _, want = item.partition("=")
        if key not in have:
            return False
        if want != "*" and have[key] != render(want, params):
            return False
    return True
