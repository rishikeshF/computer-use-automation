"""GuardedSurface and the web surface's safety features, against a fake bank origin (no network).

The fake origin is served by Playwright route fulfilment, so URLs, origins and paths are real
to the browser and the policy, but no request ever leaves the machine.
"""

import io
from collections.abc import AsyncIterator

import pytest
from PIL import Image
from playwright.async_api import Browser, Route, async_playwright

from cua.core.action import Click, Fill, Navigate
from cua.core.policy import Policy, PolicyConfig
from cua.surface.guarded import GuardedSurface
from cua.surface.web import WebSurface

ORIGIN = "https://bank.test"
PAGES = {
    "/app/form.htm": """
        <p>Account</p><input id="acct">
        <p>Password</p><input type="password" id="pw">
        <p id="echo" style="font-size:40px">Balance for 13344</p>
        <input type="submit" value="Transfer">
        <a href="/app/admin.htm">Admin</a>
        <a href="/app/other.htm">Other</a>""",
    "/app/other.htm": "<h1>Other</h1>",
    "/app/admin.htm": "<h1>Admin console</h1>",
}
POLICY = Policy(
    PolicyConfig(
        name="test",
        origins=[ORIGIN],
        allow_paths=["/app/*"],
        deny_paths=["/app/admin.htm"],
        actions=["click", "fill", "select", "navigate"],
        risk={"irreversible": [{"role": "button", "name": "transfer"}]},
        sensitive_inputs=["account"],
    )
)
MAGENTA = (255, 0, 255)  # Playwright's default mask colour


async def serve(route: Route) -> None:
    path = route.request.url.removeprefix(ORIGIN)
    if path in PAGES:
        await route.fulfill(content_type="text/html", body=PAGES[path])
    else:
        await route.fulfill(status=404, body="not found")


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


@pytest.fixture
async def web(browser: Browser) -> AsyncIterator[WebSurface]:
    context = await browser.new_context()
    await context.route(f"{ORIGIN}/**", serve)  # registered first, so the guard runs before it
    surface = WebSurface(await context.new_page(), polite_delay_ms=0, action_timeout_ms=2_000)
    await surface.guard_navigation(POLICY.url_allowed)
    yield surface
    await context.close()


def guarded(web: WebSurface) -> GuardedSurface:
    return GuardedSurface(web, POLICY, sensitive={"account_id": "13344"})


async def test_policy_blocks_admin_navigation_before_it_runs(web: WebSurface) -> None:
    g = guarded(web)
    assert (await g.act(Navigate(url=f"{ORIGIN}/app/form.htm"))).allowed
    d = await g.act(Navigate(url=f"{ORIGIN}/app/admin.htm"))
    assert d.verdict == "deny"
    assert web.url.endswith("/app/form.htm")  # never left the page
    assert web.blocked_navigations == []  # the policy stopped it; the network guard never saw it


async def test_network_guard_stops_what_policy_cannot_see(web: WebSurface) -> None:
    g = guarded(web)
    await g.act(Navigate(url=f"{ORIGIN}/app/form.htm"))
    obs = await g.observe(screenshot=False)
    # The link is named "Admin" (no deny_controls rule), so the policy allows the click as a
    # read-only link. The network guard aborts the denied page load anyway.
    d = await g.act(Click(ref=obs.find("link", "Admin")[0].ref))
    assert d.allowed
    await web._page.wait_for_timeout(200)
    assert web.blocked_navigations == [f"{ORIGIN}/app/admin.htm"]
    assert "Admin console" not in await web._page.content()


async def test_irreversible_click_waits_for_approval(web: WebSurface) -> None:
    g = guarded(web)
    await g.act(Navigate(url=f"{ORIGIN}/app/form.htm"))
    obs = await g.observe(screenshot=False)
    transfer = obs.find("button", "Transfer")[0].ref
    d = await g.act(Click(ref=transfer))
    assert d.verdict == "needs_approval" and d.risk == "irreversible"
    d = await g.act(Click(ref=transfer), irreversible_approved=True)
    assert d.allowed


async def test_sensitive_values_are_masked_in_screenshots(web: WebSurface) -> None:
    g = guarded(web)
    await g.act(Navigate(url=f"{ORIGIN}/app/form.htm"))
    obs = await g.observe(screenshot=False)
    await g.act(Fill(ref=obs.nodes[1].ref, value="13344", sensitive=True))

    def colour_at(png: bytes, selector_box: dict[str, float]) -> tuple[int, ...]:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        x = int(selector_box["x"] + selector_box["width"] / 2)
        y = int(selector_box["y"] + selector_box["height"] / 2)
        return tuple(img.getpixel((x, y)))  # type: ignore[arg-type]

    shot = (await g.observe()).screenshot
    assert shot is not None
    for selector in ("#echo", "#acct", "#pw"):  # text showing the value, the input, the password
        box = await web._page.locator(selector).bounding_box()
        assert box is not None
        assert colour_at(shot, box) == MAGENTA, selector

    # The tags used for masking are removed afterwards; the page is left as we found it.
    assert await web._page.locator("[data-cua-mask]").count() == 0

    # Without sensitive values, only the password field is masked.
    plain = (await web.observe()).screenshot
    assert plain is not None
    echo = await web._page.locator("#echo").bounding_box()
    assert echo is not None and colour_at(plain, echo) != MAGENTA


async def test_run_redactor_knows_the_run_secrets(web: WebSurface) -> None:
    g = guarded(web)
    assert g.redact("balance for 13344") == "balance for [REDACTED:account_id]"
