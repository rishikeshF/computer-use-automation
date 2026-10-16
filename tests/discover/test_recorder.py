"""Recorder: trace in, draft artifact out. Pure Python, no browser, no model."""

import pytest

from cua.core.artifact import Capability, ParamValue
from cua.core.locator import LabelText, RoleName, TableCell, TextAnchor
from cua.discover.recorder import RecorderError, TraceStep, locator_for, record
from cua.surface.base import Node, TableContext

BASE = "https://bank.test/app"


def n(ref: str, role: str, name: str = "", **kw: object) -> Node:
    kw.setdefault("bbox", (0, 0, 10, 10))
    return Node(ref=ref, role=role, name=name, **kw)


OVERVIEW = (
    n("h", "heading", "Accounts Overview"),
    n("a1", "link", "13344", table=TableContext(col="Account")),
    n("a2", "link", "54321", table=TableContext(col="Account")),
)
DETAILS = (
    n("h2", "heading", "Account Details"),
    n("c1", "cell", "Balance:", bbox=(0, 50, 80, 20)),
    n("c2", "cell", "$515.50", table=TableContext(row="Balance:"), bbox=(90, 50, 80, 20)),
    n("c3", "cell", "Available:", bbox=(0, 80, 80, 20)),
    n("c4", "cell", "$515.50", table=TableContext(row="Available:"), bbox=(90, 80, 80, 20)),
)
TRACE = [
    TraceStep(
        tool="click",
        intent="Open the account's details",
        target=OVERVIEW[1],
        page=OVERVIEW,
        url_before=f"{BASE}/overview.htm;jsessionid=XYZ",
        url_after=f"{BASE}/activity.htm;jsessionid=XYZ?id=13344",
        headings_after=("Account Details",),
    ),
    TraceStep(
        tool="extract",
        intent="Read the balance",
        value="balance",
        target=DETAILS[2],
        page=DETAILS,
        url_before=f"{BASE}/activity.htm?id=13344",
        url_after=f"{BASE}/activity.htm?id=13344",
        headings_after=("Account Details",),
    ),
]


def make(trace: list[TraceStep] = TRACE, **kw: object) -> Capability:
    args: dict[str, object] = dict(
        name="get_account_balance",
        version="0.1.0",
        goal="find the balance",
        description="Balance of one account",
        inputs={"account_id": "13344"},
        sensitive={"account_id"},
        base_url=BASE,
        vendor="test/bank",
        model="fake",
        run_id="r1",
    )
    args.update(kw)
    return record(trace, **args)  # type: ignore[arg-type]


def test_click_target_is_parameterized_and_checkpointed() -> None:
    cap = make()
    click = cap.steps[1]  # s1 is the navigate to the start page
    assert click.target is not None
    assert click.target.strategies[0] == RoleName(role="link", name="{{account_id}}")
    assert click.post.url_pattern == "/app/activity.htm?id={{account_id}}"
    assert click.post.landmark_text == "Account Details"
    assert cap.inputs[0].sensitive


def test_extract_is_found_by_position_never_by_value() -> None:
    step = make().steps[2]
    assert step.target is not None
    kinds = [s.kind for s in step.target.strategies]
    assert "role_name" not in kinds  # "$515.50" will be different next time
    # A label cell does not anchor a value cell (same role); the table position does the job.
    assert step.target.strategies == (TableCell(role="cell", row="Balance:"),)


def test_artifact_is_coherent_and_free_of_the_value() -> None:
    cap = make()
    assert cap.problems() == []
    assert cap.outputs[0].name == "balance" and cap.outputs[0].source_step == "s3"
    assert "13344" not in cap.model_dump_json()
    assert cap.content_hash == cap.behavior_hash()
    assert cap.risk == "read_only"


def test_hash_ignores_review_but_tracks_behavior() -> None:
    cap = make()
    approved = cap.model_copy(update={"status": "approved", "version": "1.0.0"})
    assert approved.behavior_hash() == cap.content_hash
    changed = make(TRACE[:1] + [TRACE[1].model_copy(update={"value": "available"})])
    assert changed.content_hash != cap.content_hash


def test_strategy_is_dropped_unless_proven_unique() -> None:
    page = (
        n("t", "text", "Amount", bbox=(0, 0, 60, 20)),
        n("i1", "textbox", label="Amount", bbox=(70, 0, 100, 20)),
        n("i2", "textbox", label="Amount", bbox=(70, 40, 100, 20)),  # same label: not unique
    )
    chain, notes = locator_for(page[1], page, {})
    assert LabelText(role="textbox", label="Amount") not in chain.strategies
    assert chain.strategies[0] == TextAnchor(role="textbox", anchor="Amount", direction="right_of")
    assert notes == ()


def test_unprovable_target_is_flagged_not_hidden() -> None:
    page = (n("b1", "button", "Edit"), n("b2", "button", "Edit"))
    chain, notes = locator_for(page[0], page, {})
    assert chain.strategies == (RoleName(role="button", name="Edit"),)
    assert notes and "no strategy uniquely matched" in notes[0]


def test_fill_values_become_params() -> None:
    page = (n("p", "textbox", label="Password", states=("secret",)),)
    fill = TraceStep(
        tool="fill",
        intent="Type the password",
        value="{{password}}",
        target=page[0],
        page=page,
        url_before=f"{BASE}/index.htm",
        url_after=f"{BASE}/index.htm",
        risk="reversible",
    )
    literal = fill.model_copy(update={"value": "alice", "intent": "Type the user"})
    cap = make(
        [literal, fill],
        inputs={"username": "alice", "password": "hunter2"},
        sensitive={"username", "password"},
    )
    assert cap.steps[1].value == ParamValue(param="username")  # literal recognized as the input
    assert cap.steps[2].value == ParamValue(param="password")
    assert cap.risk == "reversible"


def test_secret_in_typed_text_becomes_a_placeholder() -> None:
    page = (n("q", "textbox", label="Note"),)
    note = TraceStep(
        tool="fill",
        intent="Type a note",
        value="pin is hunter2!",
        target=page[0],
        page=page,
        url_before=f"{BASE}/x.htm",
        url_after=f"{BASE}/x.htm",
    )
    cap = make([note], inputs={"password": "hunter2"}, sensitive={"password"})
    assert cap.steps[1].value == "pin is {{password}}!"


def test_recorder_refuses_to_write_a_secret() -> None:
    """Templating only replaces whole tokens. A secret glued inside a word slips past it, and
    the final check must catch it rather than write it to disk."""
    page = (n("q", "textbox", label="Note"),)
    glued = TraceStep(
        tool="fill",
        intent="Type a note",
        value="xhunter2x",
        target=page[0],
        page=page,
        url_before=f"{BASE}/x.htm",
        url_after=f"{BASE}/x.htm",
    )
    with pytest.raises(RecorderError, match="sensitive input"):
        make([glued], inputs={"password": "hunter2"}, sensitive={"password"})
    with pytest.raises(RecorderError, match="nothing to record"):
        make([])


def test_navigate_uses_base_url_placeholder() -> None:
    nav = TraceStep(
        tool="navigate",
        intent="Open the overview",
        value=f"{BASE}/overview.htm;jsessionid=ABC",
        url_before=f"{BASE}/index.htm",
        url_after=f"{BASE}/overview.htm",
        headings_after=("Accounts Overview",),
    )
    step = make([nav], inputs={}, sensitive=set()).steps[0]
    assert step.value == "{{base_url}}/overview.htm"
    assert step.target is None


# ParaBank's real layout: a sidebar heading on every page, the content heading over the table.
SIDEBAR_PAGE = (
    n("side", "heading", "Account Services", bbox=(0, 100, 150, 20)),
    n("main", "heading", "Accounts Overview", bbox=(200, 100, 400, 30)),
    n("hdr", "columnheader", "Balance*", bbox=(300, 150, 100, 20)),
    n(
        "v1",
        "cell",
        "$515.50",
        table=TableContext(row="13344", col="Balance*"),
        bbox=(300, 180, 100, 20),
    ),
)


def test_fallbacks_that_ignore_the_input_are_dropped() -> None:
    """With one account, "the cell below Balance*" is unique, but it would read the first row
    for any account. A chain found by {{account_id}} keeps only strategies that use it."""
    chain, _ = locator_for(SIDEBAR_PAGE[3], SIDEBAR_PAGE, {"account_id": "13344"}, reading=True)
    assert chain.strategies == (TableCell(role="cell", row="{{account_id}}", col="Balance*"),)


def test_landmark_is_the_section_heading_not_the_sidebar() -> None:
    read = TraceStep(
        tool="extract",
        intent="Read the balance",
        value="balance",
        target=SIDEBAR_PAGE[3],
        page=SIDEBAR_PAGE,
        url_before=f"{BASE}/overview.htm",
        url_after=f"{BASE}/overview.htm",
        headings_after=("Account Services", "Accounts Overview"),
    )
    cap = make([read])
    assert cap.success.landmark_text == "Accounts Overview"
    assert cap.steps[0].post.landmark_text == "Accounts Overview"  # the start page, checked
    assert cap.steps[1].pre == cap.steps[0].post
    assert cap.steps[1].post.landmark_text is None  # reading does not change the page


# The real ParaBank overview, as the snapshot sees it: header row, then one row per account.
PARABANK = (
    n("side", "heading", "Account Services", bbox=(0, 100, 150, 20)),
    n("main", "heading", "Accounts Overview", bbox=(200, 100, 400, 30)),
    n("h1", "columnheader", "Account", bbox=(200, 150, 100, 20)),
    n("h2", "columnheader", "Balance*", bbox=(300, 150, 100, 20)),
    n("acct", "link", "13344", table=TableContext(col="Account"), bbox=(200, 180, 100, 20)),
    n(
        "bal",
        "cell",
        "$515.50",
        table=TableContext(row="13344", col="Balance*"),
        bbox=(300, 180, 100, 20),
    ),
)
READ_BALANCE = TraceStep(
    tool="extract",
    intent="Read the balance",
    value="balance",
    target=PARABANK[5],
    page=PARABANK,
    url_before=f"{BASE}/overview.htm",
    url_after=f"{BASE}/overview.htm",
    headings_after=("Account Services", "Accounts Overview"),
)


def test_account_not_found_is_declared_with_a_guard() -> None:
    cap = make([READ_BALANCE])
    (d,) = cap.outcomes
    assert d.code == "account_not_found"
    assert d.check_before == ("s2",)
    assert d.when.page == cap.steps[1].pre  # only meaningful on the right page
    assert d.when.absent[0].strategies == (RoleName(role="link", name="{{account_id}}"),)
    # The headers must be there, so a reshaped table is a Failure, not "account not found".
    # Headers alone are not enough: the rows may still be loading. A Balance* data cell is.
    assert [g.strategies[0] for g in d.when.present] == [
        RoleName(role="columnheader", name="Account"),
        RoleName(role="columnheader", name="Balance*"),
        TableCell(role="cell", row="*", col="Balance*"),
    ]
    assert "13344" not in cap.model_dump_json()
    assert cap.problems() == []


def test_no_detector_without_a_guard() -> None:
    """TRACE's overview has no header row, so an absence could not be told apart from a broken
    page. The recorder leaves the outcome to a reviewer rather than guess."""
    assert make().outcomes == ()


def test_amount_output_is_typed() -> None:
    (out,) = make([READ_BALANCE]).outputs
    assert (out.type, out.unit) == ("decimal", "USD")


# ParaBank's login form as the snapshot sees it (boxes from the live site, 2026-10-15).
LOGIN_FORM = (
    n("sol", "text", "Solutions", bbox=(293, 76, 137, 23)),
    n("cl", "heading", "Customer Login", bbox=(293, 251, 164, 34)),
    n("ut", "text", "Username", bbox=(293, 287, 63, 15)),
    n("u", "textbox", label="Username", bbox=(293, 305, 146, 18)),
    n("pt", "text", "Password", bbox=(293, 335, 61, 15)),
    n("p", "textbox", label="Password", states=("secret",), bbox=(293, 353, 146, 18)),
    n("b", "button", "Log In", bbox=(293, 382, 66, 19)),
)


def test_text_anchor_only_from_the_text_that_labels_the_control() -> None:
    """The field's own label is kept. "Customer Login" and "Solutions" are above it too, but
    the label sits in between, so they describe the page, not the field."""
    chain, _ = locator_for(LOGIN_FORM[3], LOGIN_FORM, {})
    assert chain.strategies == (
        LabelText(role="textbox", label="Username"),
        TextAnchor(role="textbox", anchor="Username", direction="below"),
    )


def test_button_is_not_anchored_to_another_fields_label() -> None:
    """ "Password" is above Log In, but the password field is closer: it is that field's label."""
    chain, _ = locator_for(LOGIN_FORM[6], LOGIN_FORM, {})
    assert chain.strategies == (RoleName(role="button", name="Log In"),)


def test_distant_text_is_not_an_anchor() -> None:
    page = (
        n("t", "text", "Amount", bbox=(0, 0, 60, 20)),
        n("i", "textbox", bbox=(400, 0, 100, 20)),  # 340 px to the right: not its label
    )
    chain, notes = locator_for(page[1], page, {})
    assert not any(isinstance(s, TextAnchor) for s in chain.strategies)
    assert notes  # nothing could be proven, and the review notes say so
