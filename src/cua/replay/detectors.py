"""App-level detectors: is the app itself in a state no step of any capability expects?

Two of them, checked in this order:
  1. session_expired   the login form is showing. Recoverable: sign in again, resume.
  2. app_error         an HTTP 5xx, or the app's own error page. Retried on page loads,
                       otherwise a Failure the caller sees.

Order matters on ParaBank: with no session it shows the login form AND "An internal error has
occurred" on the same page. Checked the other way round, every expired session would look like a
server fault.

Capability-level detectors (account_not_found) are declared in the artifact and evaluated by the
engine; these are the ones every capability of the app shares. Pure functions over an
Observation, so they are tested without a browser.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cua.core.artifact import Checkpoint
from cua.core.url import canonical_path, url_matches
from cua.surface.base import Observation

AppIssue = Literal["session_expired", "app_error"]


@dataclass(frozen=True)
class AppSignals:
    """What trouble looks like in this app. ParaBank's defaults; another app passes its own."""

    login_button: str = "Log In"
    error_heading: str = "Error!"
    error_text: str = "internal error"


PARABANK = AppSignals()


def app_issue(
    obs: Observation, signals: AppSignals = PARABANK, *, check_session: bool = True
) -> tuple[AppIssue, str] | None:
    """(issue, evidence) or None when the app looks healthy.

    `check_session` is off while signing in (ensure_session runs on the login page by design)."""
    if check_session and _login_form(obs, signals):
        return "session_expired", "the login form is showing"
    if obs.http_status is not None and obs.http_status >= 500:
        return "app_error", f"HTTP {obs.http_status} on {canonical_path(obs.url)}"
    if any(n.role == "heading" and n.name == signals.error_heading for n in obs.nodes):
        said = next(
            (n.name for n in obs.nodes if signals.error_text in n.name.lower()),
            signals.error_heading,
        )
        return "app_error", f"error page on {canonical_path(obs.url)}: {said}"
    return None


def _login_form(obs: Observation, signals: AppSignals) -> bool:
    has_button = any(n.role == "button" and n.name == signals.login_button for n in obs.nodes)
    has_secret = any("secret" in n.states for n in obs.nodes)
    return has_button and has_secret


def checkpoint_gap(cp: Checkpoint, obs: Observation, params: Mapping[str, str]) -> str | None:
    """Why `cp` does not hold on `obs`, or None when it holds. element_present is checked by the
    engine (it needs the surface to resolve a locator)."""
    if cp.url_pattern and not url_matches(obs.url, cp.url_pattern, params):
        return f"expected page {cp.url_pattern}, on {canonical_path(obs.url)}"
    if cp.landmark_text and not any(n.name == cp.landmark_text for n in obs.nodes):
        return f"expected {cp.landmark_text!r} on the page, not there"
    return None
