"""Surface-independent locator matching over snapshot Nodes.

Any surface that can produce Nodes (web today, desktop UI Automation later) gets the robust
strategies for free: RoleName, LabelText and TextAnchor are matched here, in plain Python, against
the same nodes the model saw during discovery. That keeps perception and resolution consistent:
a control is found on replay by exactly the description that was recorded.

Css and BBox depend on the surface's own element model, so each surface handles those itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from cua.core.locator import LabelText, RoleName, TextAnchor
from cua.surface.base import Node

# Two candidates closer than this (in px) to the anchor count as a tie, so the match is ambiguous.
_TIE_PX = 1.0
# How far a candidate may overlap the anchor's edge and still count as "beside" it.
_EDGE_SLACK_PX = 2.0


def match_nodes(strategy: RoleName | LabelText | TextAnchor, nodes: Sequence[Node]) -> list[Node]:
    """Return every node the strategy matches. The caller wants exactly one."""
    if isinstance(strategy, RoleName):
        return [n for n in nodes if n.role == strategy.role and n.name == strategy.name]
    if isinstance(strategy, LabelText):
        return [n for n in nodes if n.role == strategy.role and n.label == strategy.label]
    return _match_anchor(strategy, nodes)


def anchor_note(strategy: TextAnchor, nodes: Sequence[Node]) -> str | None:
    """Explain an anchor failure that is about the anchor text itself, not the target."""
    count = sum(1 for n in nodes if n.name == strategy.anchor and n.role != strategy.role)
    if count == 0:
        return f"anchor text {strategy.anchor!r} not on screen"
    if count > 1:
        return f"anchor text {strategy.anchor!r} appears {count} times"
    return None


def _match_anchor(s: TextAnchor, nodes: Sequence[Node]) -> list[Node]:
    anchors = [n for n in nodes if n.name == s.anchor and n.role != s.role]
    if len(anchors) != 1:
        return []  # no anchor, or an ambiguous anchor: refuse to guess
    a = anchors[0]
    scored = sorted(
        (
            (d, n)
            for n in nodes
            if n.role == s.role and n.frame == a.frame
            if (d := _distance(a, n, s.direction)) is not None
        ),
        key=lambda pair: pair[0],
    )
    if not scored:
        return []
    best = [scored[0][1]]
    # Report ties so the caller sees "ambiguous" instead of silently picking one.
    best += [n for d, n in scored[1:] if d - scored[0][0] < _TIE_PX]
    return best


def _distance(a: Node, n: Node, direction: str) -> float | None:
    """Gap from anchor a to candidate n in the given direction, or None if n is not that way."""
    ax, ay, aw, ah = a.bbox
    nx, ny, nw, nh = n.bbox
    overlaps_rows = ny < ay + ah and ay < ny + nh
    overlaps_cols = nx < ax + aw and ax < nx + nw
    if direction == "right_of" and overlaps_rows and nx >= ax + aw - _EDGE_SLACK_PX:
        return nx - (ax + aw)
    if direction == "left_of" and overlaps_rows and nx + nw <= ax + _EDGE_SLACK_PX:
        return ax - (nx + nw)
    if direction == "below" and overlaps_cols and ny >= ay + ah - _EDGE_SLACK_PX:
        return ny - (ay + ah)
    if direction == "above" and overlaps_cols and ny + nh <= ay + _EDGE_SLACK_PX:
        return ay - (ny + nh)
    return None
