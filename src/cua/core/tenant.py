"""Tenants: one recorded artifact, many institutions running the same vendor app.

Tenants of the same product differ in configuration, not in flow: the same pages and steps, with
their own words on the buttons and headings ("Accounts Overview" here, "Account Summary" there).
So an artifact is recorded once, on a base install, and a tenant gets a small override:

    tenants/<tenant>.yaml
        vendor        the product it applies to (must match the artifact's app.vendor)
        text          UI text as the base artifacts know it -> as this tenant shows it
        signals       how this tenant shows trouble (its login button, its error heading)
        fingerprints  this tenant's navigation fingerprint per start page, so drift on it is
                      still noticed ("/overview.htm" for artifacts that open on the overview)

One override serves every capability of the app, preconditions included: renaming "Log In" once
fixes sign-in for all of them. apply() derives the tenant's effective artifact. The base file is
never edited: the effective one gets its own id (`name@version+tenant`) and its own hash, and
records the base hash it was derived from, so evidence says exactly what ran.

Only UI text is renamed, and only whole values: a locator's name, label, anchor, table row or
column, and checkpoint landmarks. Placeholders, URLs, codes and messages are never touched.

Variant (below) is the other side, for demos and tests: the words a simulated tenant uses. The
browser side that applies them is cua.surface.variant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from cua.core.artifact import Capability
from cua.core.template import render
from cua.core.url import canonical_path

# Keys of a locator strategy that hold UI text (as opposed to kind, role, direction, ...).
_STRATEGY_TEXT = {"name", "label", "anchor", "row", "col"}


class TenantOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    vendor: str
    text: dict[str, str] = {}
    signals: dict[str, str] = {}  # AppSignals fields: login_button, error_heading, error_text
    # Start path (relative to the app root) -> nav_fingerprint of that page on this tenant.
    fingerprints: dict[str, str] = {}
    note: str = ""

    @classmethod
    def load(cls, path: str | Path) -> TenantOverride:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class OverrideError(Exception):
    """The override does not fit the artifact (another vendor's app)."""


def apply(cap: Capability, ov: TenantOverride) -> tuple[Capability, list[str]]:
    """The tenant's effective artifact, and the renames that were used ("old -> new")."""
    if ov.vendor != cap.app.vendor:
        raise OverrideError(f"{ov.tenant} overrides {ov.vendor}, not {cap.app.vendor}")
    used: set[str] = set()

    def rename(value: Any) -> Any:
        if isinstance(value, str) and value in ov.text:
            used.add(value)
            return ov.text[value]
        return value

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, dict):
            strategy = "kind" in node
            return {
                k: rename(v)
                if (strategy and k in _STRATEGY_TEXT) or k == "landmark_text"
                else walk(v)
                for k, v in node.items()
            }
        return node

    body = cap.model_dump(mode="json")
    for part in ("steps", "success", "outcomes"):
        body[part] = walk(body[part])
    body["id"] = f"{cap.name}@{cap.version}+{ov.tenant}"
    # The fingerprint of the page this artifact opens on, as this tenant shows it. None when the
    # override does not know that page: no check beats a check against another page.
    start = start_path(cap)
    body["app"]["app_version_fingerprint"] = ov.fingerprints.get(start) if start else None
    body["overrides"] = {"tenant": ov.tenant, "base": cap.id, "base_hash": cap.content_hash}
    effective = Capability.model_validate(body).sealed()
    return effective, [f"{k} -> {ov.text[k]}" for k in sorted(used)]


def start_path(cap: Capability) -> str | None:
    """The page step s1 opens, relative to the app root ("/overview.htm"), or None when s1 is
    not a navigate."""
    first = cap.steps[0]
    if first.action != "navigate":
        return None
    try:
        return canonical_path(render(str(first.value), {cap.app.base_url_param: ""}))
    except KeyError:  # a start page that depends on an input has no single fingerprint
        return None


# ---- making the base app look like a tenant (demos and tests) -----------------------------------


class Variant(BaseModel):
    """tenants/variants/<tenant>.yaml: the words a simulated tenant's pages use instead."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    text: dict[str, str]  # visible text on the base app -> on this tenant
    note: str = ""

    @classmethod
    def load(cls, path: str | Path) -> Variant:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
