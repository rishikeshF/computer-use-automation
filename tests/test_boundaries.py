"""Architecture rules from CLAUDE.md, enforced by reading imports (not by trusting reviewers).

1. Replay never reaches the LLM, not even transitively. Production replay must run with no model.
2. Only cua.surface touches Playwright. Everything else works against the Surface protocol.
3. Only discovery (and the CLI that wires it) talks to the model.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


GRAPH = {module_name(p): imports_of(p) for p in SRC.rglob("*.py")}


def reachable_from(prefix: str) -> set[str]:
    """All cua modules (and external imports) reachable from modules under `prefix`."""
    seen: set[str] = set()
    todo = [m for m in GRAPH if m == prefix or m.startswith(prefix + ".")]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for imp in GRAPH.get(mod, set()):
            # "from cua.core.action import X" names the module itself; follow it and its parents.
            todo += [m for m in GRAPH if imp == m or imp.startswith(m + ".")]
            seen.add(imp)
    return seen


def test_replay_never_reaches_llm() -> None:
    bad = {
        m
        for m in reachable_from("cua.replay")
        if m.startswith(("cua.llm", "cua.discover", "anthropic", "openai"))
    }
    assert not bad, f"replay reaches LLM code: {sorted(bad)}"


def test_only_surface_imports_playwright() -> None:
    offenders = [
        mod
        for mod, imps in GRAPH.items()
        if not mod.startswith("cua.surface") and any(i.startswith("playwright") for i in imps)
    ]
    assert not offenders, f"Playwright used outside cua.surface: {offenders}"


def test_core_depends_on_nothing_else_in_cua() -> None:
    """core holds the shared types; every other layer depends on it, never the reverse."""
    leaks = {
        mod: sorted(i for i in imps if i.startswith("cua.") and not i.startswith("cua.core"))
        for mod, imps in GRAPH.items()
        if mod.startswith("cua.core")
    }
    assert not any(leaks.values()), f"core imports other layers: {leaks}"


def test_only_discovery_talks_to_the_model() -> None:
    """The model is a discovery-time tool. Core, surface and (later) replay and handoff never
    reach it; the CLI wires it into discovery and nothing else."""
    allowed = ("cua.llm", "cua.discover", "cua.cli")
    offenders = sorted(
        mod
        for mod, imps in GRAPH.items()
        if not mod.startswith(allowed) and any(i.startswith(("cua.llm", "anthropic")) for i in imps)
    )
    assert not offenders, f"model reached from outside discovery: {offenders}"
