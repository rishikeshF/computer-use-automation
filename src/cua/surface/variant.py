"""Tenant simulation in our own browser: make the base app look like another tenant's install.

A script runs in every page as it loads and renames visible text (whole text nodes and button
labels) from the variant's map, then keeps doing it for anything added later (rows filled in by
a request). Attributes and form fields are never touched, so the app gets exactly the same
requests: only what the automation sees changes. A stand-in for a second institution running
the same vendor product, without a second server.

Done in the page rather than by rewriting responses: it survives redirects (the browser follows
those without asking us) and works on recorded offline packs too.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from playwright.async_api import BrowserContext

_RENAME_JS = """(text) => {
  const has = (t) => Object.prototype.hasOwnProperty.call(text, t);
  const swap = (root) => {
    if (!root) return;
    if (root.nodeType === 3) {
      const t = root.nodeValue.trim();
      if (has(t)) root.nodeValue = root.nodeValue.replace(t, text[t]);
      return;
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) swap(n);
    const buttons = root.querySelectorAll ? root.querySelectorAll(
      "input[type=submit], input[type=button]") : [];
    for (const b of buttons) if (has(b.value)) b.value = text[b.value];
    if (root.matches && root.matches("input[type=submit], input[type=button]") && has(root.value)) {
      root.value = text[root.value];
    }
  };
  const start = () => {
    swap(document.body);
    new MutationObserver((changes) => {
      for (const c of changes) for (const n of c.addedNodes) swap(n);
    }).observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
}"""


async def install_variant(context: BrowserContext, text: Mapping[str, str]) -> None:
    await context.add_init_script(f"({_RENAME_JS})({json.dumps(dict(text))})")
