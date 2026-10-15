"""URL normalization shared by policy (what may we touch) and redaction (what may we write down).

Legacy Java apps put the session id in the path (`index.htm;jsessionid=8F58...`). That is a
session token: it must never reach a log, and it must never stop a path rule from matching.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urlsplit

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
