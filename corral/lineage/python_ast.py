"""Shared AST helpers for the lineage extractors.

These helpers exist so that :mod:`corral.lineage.extract_sql` and
:mod:`corral.lineage.extract_files` can share a consistent view of the
Python source they walk: same string-literal flattening (incl. f-strings),
same notion of "which top-level symbol owns this lineno".
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class TopLevelOwner:
    name: str
    start: int
    end: int


def top_level_owners(tree: ast.Module) -> list[TopLevelOwner]:
    """Return top-level ``def``/``async def``/``class`` line ranges.

    Methods on a class are attributed to the *class* (the code-map symbol
    table works the same way), which keeps the symbol resolution aligned
    with ``symbols.parquet``.
    """

    owners: list[TopLevelOwner] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno
            owners.append(TopLevelOwner(stmt.name, stmt.lineno, end))
    return owners


def owner_for_lineno(owners: list[TopLevelOwner], lineno: int) -> str | None:
    """Return the name of the top-level symbol containing ``lineno``."""

    for owner in owners:
        if owner.start <= lineno <= owner.end:
            return owner.name
    return None


def source_with_symbol(rel_path: str, symbol: str | None) -> tuple[str, str]:
    """Return ``(src_kind, src)`` for an edge originating at ``rel_path``.

    - With ``symbol``: ``("symbol", "rel/path.py:symbol")``
    - Without:        ``("module", "rel/path.py")``
    """

    if symbol:
        return "symbol", f"{rel_path}:{symbol}"
    return "module", rel_path


def flatten_string_node(node: ast.AST) -> str | None:
    """Best-effort string value for an AST node.

    - ``ast.Constant`` of type ``str``: the value
    - ``ast.JoinedStr`` (f-string): constant parts joined; interpolated
      ``{expr}`` slots are replaced with a single space so the surrounding
      regexes do not match through them.
    - Anything else: ``None``.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(" ")
        return "".join(parts)
    return None


def call_attr_chain(call: ast.Call) -> tuple[str, ...]:
    """Return the dotted attribute chain of a Call's ``.func``.

    ``pd.read_parquet(...)`` → ``("pd", "read_parquet")``;
    ``open(...)``             → ``("open",)``;
    ``conn.execute(...)``     → ``("conn", "execute")``;
    ``df.to_parquet(...)``    → ``("df", "to_parquet")``.

    Returns the empty tuple for shapes we cannot statically resolve
    (e.g. ``foo()()``).
    """

    parts: list[str] = []
    node: ast.AST = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()
