"""Pure locator matching over Nodes. No browser needed: any surface that yields Nodes gets this."""

from cua.core.locator import LabelText, RoleName, TableCell, TextAnchor
from cua.surface.base import Node, TableContext
from cua.surface.matching import anchor_note, match_nodes


def node(
    ref: str,
    role: str,
    name: str = "",
    *,
    label: str | None = None,
    bbox: tuple[float, float, float, float] = (0, 0, 10, 10),
) -> Node:
    return Node(ref=ref, role=role, name=name, label=label, bbox=bbox)


# A legacy form row: "Amount:" text, then an input to its right, then another input further right.
ROW = [
    node("t1", "text", "Amount", bbox=(0, 100, 60, 20)),
    node("i1", "textbox", label="Amount", bbox=(70, 100, 150, 20)),
    node("i2", "textbox", label="Memo", bbox=(300, 100, 150, 20)),
    node("b1", "button", "Edit"),
    node("b2", "button", "Edit"),
]


def test_role_name_exact() -> None:
    assert [n.ref for n in match_nodes(RoleName(role="textbox", name="Amount"), ROW)] == []
    assert len(match_nodes(RoleName(role="button", name="Edit"), ROW)) == 2  # caller sees ambiguity


def test_label_text_matches_inferred_label_only() -> None:
    assert [n.ref for n in match_nodes(LabelText(role="textbox", label="Amount"), ROW)] == ["i1"]


def test_text_anchor_picks_nearest_in_direction() -> None:
    s = TextAnchor(role="textbox", anchor="Amount", direction="right_of")
    assert [n.ref for n in match_nodes(s, ROW)] == ["i1"]
    assert match_nodes(TextAnchor(role="textbox", anchor="Amount", direction="left_of"), ROW) == []


def test_text_anchor_reports_ties() -> None:
    nodes = [
        node("t", "text", "Type", bbox=(0, 0, 40, 60)),
        node("a", "combobox", bbox=(50, 0, 100, 20)),
        node("b", "combobox", bbox=(50, 40, 100, 20)),  # same gap to the anchor: a tie
    ]
    assert (
        len(match_nodes(TextAnchor(role="combobox", anchor="Type", direction="right_of"), nodes))
        == 2
    )


def test_text_anchor_refuses_ambiguous_anchor() -> None:
    nodes = ROW + [node("t2", "text", "Amount", bbox=(0, 300, 60, 20))]
    s = TextAnchor(role="textbox", anchor="Amount", direction="right_of")
    assert match_nodes(s, nodes) == []
    assert anchor_note(s, nodes) == "anchor text 'Amount' appears 2 times"


def test_table_cell_matches_row_and_column() -> None:
    cells = [
        Node(
            ref="a",
            role="cell",
            name="$515.50",
            table=TableContext(row="Balance:"),
            bbox=(0, 0, 1, 1),
        ),
        Node(
            ref="b",
            role="cell",
            name="$515.50",
            table=TableContext(row="Available:"),
            bbox=(0, 0, 1, 1),
        ),
    ]
    assert [n.ref for n in match_nodes(TableCell(role="cell", row="Balance:"), cells)] == ["a"]
    assert match_nodes(TableCell(role="cell", row="Balance:", col="Amount"), cells) == []


def test_text_anchor_ignores_candidates_beyond_the_gap_cap() -> None:
    far = [
        node("t", "text", "Amount", bbox=(0, 0, 60, 20)),
        node("i", "textbox", bbox=(300, 0, 100, 20)),  # 240 px away: not labelled by "Amount"
    ]
    assert match_nodes(TextAnchor(role="textbox", anchor="Amount", direction="right_of"), far) == []


def test_text_anchor_can_match_part_of_a_longer_text() -> None:
    nodes = [
        node(
            "p",
            "text",
            "A minimum of $100.00 must be deposited. Please choose an existing account.",
            bbox=(0, 0, 400, 30),
        ),
        node("c", "combobox", bbox=(0, 40, 140, 18)),
    ]
    short = "Please choose an existing account."
    exact = TextAnchor(role="combobox", anchor=short, direction="below")
    part = TextAnchor(role="combobox", anchor=short, direction="below", contains=True)
    assert match_nodes(exact, nodes) == []
    assert [n.ref for n in match_nodes(part, nodes)] == ["c"]
    assert "contains" not in exact.model_dump()  # left out when False: old hashes stay valid
