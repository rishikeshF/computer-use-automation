"""WebSurface against local HTML (no network): snapshot, resolve, act.

The fixtures imitate legacy markup on purpose: no <label>, table layouts, frames, no test IDs.
"""

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Browser, async_playwright

from cua.core.action import Click, Fill
from cua.core.locator import BBox, Css, LabelText, LocatorChain, RoleName, TextAnchor
from cua.surface.base import Resolved, SurfaceError, Unresolved
from cua.surface.web import WebSurface

LEGACY_LOGIN = """
<form>
  <p><b>Username</b></p><div><input type="text" name="u"></div>
  <p><b>Password</b></p><div><input type="password" name="p"></div>
  <div><input type="submit" value="Log In"></div>
  <input type="hidden" name="csrf" value="x">
  <div style="display:none"><button>Hidden</button></div>
</form>
"""

ACCOUNTS = """
<table>
  <tr><th>Account</th><th>Balance</th></tr>
  <tr><td><a href="#a">13344</a></td><td>$515.50</td></tr>
  <tr><td><a href="#b">13355</a></td><td>$20.00</td></tr>
</table>
<button>Edit</button><button>Edit</button>
"""

TABLE_FORM = """
<table>
  <tr><td>Amount:</td><td><input name="amt"></td></tr>
  <tr><td>Type:</td><td><select><option>CHECKING</option><option>SAVINGS</option></select></td></tr>
</table>
<input type="checkbox" id="c"> Remember me
"""


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


@pytest.fixture
async def surface(browser: Browser) -> AsyncIterator[WebSurface]:
    page = await browser.new_page()
    yield WebSurface(page, polite_delay_ms=0, action_timeout_ms=2_000)
    await page.close()


async def load(s: WebSurface, html: str) -> None:
    await s._page.set_content(html)


# ---- observe ------------------------------------------------------------------------------------


async def test_legacy_inputs_get_inferred_labels(surface: WebSurface) -> None:
    await load(surface, LEGACY_LOGIN)
    obs = await surface.observe()
    boxes = [n for n in obs.nodes if n.role == "textbox"]
    assert [(n.name, n.label) for n in boxes] == [("", "Username"), ("", "Password")]
    assert "secret" in boxes[1].states
    assert obs.find("button", "Log In")
    assert not obs.find("button", "Hidden")  # invisible controls are not offered
    assert obs.screenshot and obs.screenshot.startswith(b"\x89PNG")


async def test_secret_values_are_never_read(surface: WebSurface) -> None:
    await load(surface, LEGACY_LOGIN)
    obs = await surface.observe(screenshot=False)
    user, pw = [n for n in obs.nodes if n.role == "textbox"]
    await surface.act(Fill(ref=user.ref, value="alice"))
    await surface.act(Fill(ref=pw.ref, value="hunter2", sensitive=True))
    obs = await surface.observe(screenshot=False)
    assert next(n for n in obs.nodes if n.ref == user.ref).value == "alice"
    assert next(n for n in obs.nodes if n.ref == pw.ref).value is None
    assert "hunter2" not in obs.model_dump_json()


async def test_table_cells_carry_row_and_column(surface: WebSurface) -> None:
    await load(surface, ACCOUNTS)
    obs = await surface.observe(screenshot=False)
    cell = next(n for n in obs.nodes if n.name == "$20.00")
    assert (
        cell.role == "cell"
        and cell.table
        and (cell.table.row, cell.table.col) == ("13355", "Balance")
    )
    header = next(n for n in obs.nodes if n.name == "Balance")
    assert header.role == "columnheader" and header.table is None  # header row has no row key


async def test_table_form_and_checkbox_labels(surface: WebSurface) -> None:
    await load(surface, TABLE_FORM)
    obs = await surface.observe(screenshot=False)
    assert next(n for n in obs.nodes if n.role == "textbox").label == "Amount"
    select = next(n for n in obs.nodes if n.role == "combobox")
    assert (select.label, select.options, select.value) == (
        "Type",
        ("CHECKING", "SAVINGS"),
        "CHECKING",
    )
    assert next(n for n in obs.nodes if n.role == "checkbox").label == "Remember me"


async def test_refs_are_stable_and_unique_across_observations(surface: WebSurface) -> None:
    await load(surface, LEGACY_LOGIN)
    first = await surface.observe(screenshot=False)
    second = await surface.observe(screenshot=False)
    assert [n.ref for n in first.nodes] == [n.ref for n in second.nodes]
    await load(surface, LEGACY_LOGIN)  # new document: new refs, old ones must not be reused
    third = await surface.observe(screenshot=False)
    assert not {n.ref for n in first.nodes} & {n.ref for n in third.nodes}


async def test_frames_are_observed_with_their_path(surface: WebSurface) -> None:
    await load(surface, '<iframe name="main" srcdoc="<button>Inside</button>"></iframe>')
    await surface._page.frame(name="main").wait_for_load_state()  # type: ignore[union-attr]
    obs = await surface.observe(screenshot=False)
    inside = obs.find("button", "Inside")[0]
    assert inside.frame == ("main",)
    r = await surface.resolve(
        LocatorChain(frame=("main",), strategies=(RoleName(role="button", name="Inside"),))
    )
    assert isinstance(r, Resolved) and r.ref == inside.ref
    await surface.act(Click(ref=r.ref))  # acting inside a frame works through the ref


# ---- resolve ------------------------------------------------------------------------------------


async def test_resolve_falls_back_and_records_attempts(surface: WebSurface) -> None:
    await load(surface, LEGACY_LOGIN)
    chain = LocatorChain(
        strategies=(
            RoleName(role="textbox", name="Username"),  # legacy markup: no accessible name
            LabelText(role="textbox", label="Username"),
        )
    )
    r = await surface.resolve(chain)
    assert isinstance(r, Resolved)
    assert r.strategy_index == 1
    assert [(a.kind, a.matches) for a in r.attempts] == [("role_name", 0), ("label_text", 1)]


async def test_resolve_reports_ambiguous_instead_of_guessing(surface: WebSurface) -> None:
    await load(surface, ACCOUNTS)
    r = await surface.resolve(LocatorChain(strategies=(RoleName(role="button", name="Edit"),)))
    assert isinstance(r, Unresolved) and r.reason == "ambiguous"


async def test_resolve_not_found_and_missing_frame(surface: WebSurface) -> None:
    await load(surface, ACCOUNTS)
    r = await surface.resolve(LocatorChain(strategies=(RoleName(role="link", name="99999"),)))
    assert isinstance(r, Unresolved) and r.reason == "not_found"
    r = await surface.resolve(
        LocatorChain(frame=("nope",), strategies=(RoleName(role="link", name="x"),))
    )
    assert isinstance(r, Unresolved) and r.reason == "frame_not_found"


async def test_resolve_waits_for_late_elements(surface: WebSurface) -> None:
    await load(
        surface,
        "<script>setTimeout(() => document.body.innerHTML = '<button>Late</button>', 400)</script>",
    )
    chain = LocatorChain(strategies=(RoleName(role="button", name="Late"),))
    assert isinstance(await surface.resolve(chain), Unresolved)
    assert isinstance(await surface.resolve(chain, timeout_ms=3_000), Resolved)


async def test_text_anchor_and_css_fallbacks(surface: WebSurface) -> None:
    await load(surface, TABLE_FORM)
    anchor = await surface.resolve(
        LocatorChain(
            strategies=(TextAnchor(role="textbox", anchor="Amount:", direction="right_of"),)
        )
    )
    assert isinstance(anchor, Resolved)
    css = await surface.resolve(
        LocatorChain(
            strategies=(
                BBox(x=0, y=0, width=10, height=10),  # never used on web
                Css(selector="input[name=amt]"),
            )
        )
    )
    assert isinstance(css, Resolved) and css.strategy_index == 1 and css.ref == anchor.ref
    assert css.attempts[0].note and "bbox" in css.attempts[0].note


async def test_inline_text_anchors_the_link_beside_it(surface: WebSurface) -> None:
    """The text's box is its own, not its paragraph's, so it sits left of the link inside it."""
    await load(surface, "<p>Your new account number: <a href='#'>13566</a></p>")
    found = await surface.resolve(
        LocatorChain(
            strategies=(
                TextAnchor(role="link", anchor="Your new account number:", direction="right_of"),
            )
        )
    )
    assert isinstance(found, Resolved) and found.node is not None
    assert found.node.name == "13566"


# ---- act ----------------------------------------------------------------------------------------


async def test_acting_on_a_stale_ref_raises(surface: WebSurface) -> None:
    await load(surface, LEGACY_LOGIN)
    ref = (await surface.observe(screenshot=False)).find("button", "Log In")[0].ref
    await load(surface, "<p>gone</p>")
    with pytest.raises(SurfaceError) as e:
        await surface.act(Click(ref=ref))
    assert e.value.kind == "stale_ref"
