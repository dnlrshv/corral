"""Detect ``reads_config`` edges: Python code → ``<config file>::key.path``.

Scans Python AST for calls to known config-loader functions and tracks
subsequent key accesses (``.get("key")`` and ``["key"]``) on the returned
dicts. One ``reads_config`` edge is emitted per detected access site.

The set of known loaders is not hardcoded: it is supplied by the caller as
a ``loader name -> config file path`` mapping, read from ``corral.yaml``
(``lineage.config_loaders``, default empty). An optional
``loader name -> key prefix`` mapping (``lineage.config_loader_key_prefixes``)
declares keys that wrapper loaders have already traversed.

Design notes
------------
- Binding scope is the top-level function / class body.  Bindings do not
  propagate into nested ``def``/``class`` bodies (they get their own scope).
- Sub-bindings are tracked: ``sub = cfg.get("a")`` followed by
  ``sub.get("b")`` emits ``reads_config`` for ``::a.b``.
- Inline chaining ``cfg.get("a", {}).get("b")`` also emits both ``::a`` and
  ``::a.b`` because the recursive chain-checker visits each level of the call
  chain.
- ``yaml.safe_load()`` calls with dynamic path args are not tracked (too noisy
  and unreliable); use the named loaders for traceability.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path

from .python_ast import owner_for_lineno, source_with_symbol, top_level_owners
from .schema import Edge

logger = logging.getLogger(__name__)

# Binding: variable_name → (config_file, key_prefix)
# key_prefix is the dotted key path already traversed (empty string for root)
_Bindings = dict[str, tuple[str, str]]


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _call_name(call: ast.Call) -> str | None:
    """Return the bare function name for a call expression."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _loader_binding(
    call: ast.Call,
    loaders: Mapping[str, str],
    key_prefixes: Mapping[str, str],
) -> tuple[str, str] | None:
    """Return ``(config_file, key_prefix)`` for a known loader call."""
    name = _call_name(call)
    if name is None:
        return None
    cfg_file = loaders.get(name)
    if cfg_file is None:
        return None
    return cfg_file, key_prefixes.get(name, "")


def _subscript_string_key(node: ast.Subscript) -> str | None:
    """Return the string key if the subscript uses a string literal, else None.

    Handles both Python 3.8 (``ast.Index`` wrapper) and 3.9+ (direct slice).
    """
    slice_node = node.slice
    # Python 3.8 compatibility: unwrap ast.Index
    if isinstance(slice_node, ast.Index):
        slice_node = slice_node.value  # type: ignore[attr-defined]
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return None


def _config_chain(
    node: ast.AST,
    bindings: _Bindings,
    loaders: Mapping[str, str],
    key_prefixes: Mapping[str, str],
) -> tuple[str, list[str]] | None:
    """Walk a call/subscript chain and return ``(config_file, [key_parts])``.

    Returns ``None`` if the chain does not root back to a config-bound variable.
    Each call recurses one level deeper; the key parts accumulate bottom-up.
    """
    if isinstance(node, ast.Name):
        binding = bindings.get(node.id)
        if binding is None:
            return None
        cfg_file, key_prefix = binding
        return cfg_file, [key_prefix] if key_prefix else []

    if isinstance(node, ast.Call):
        loader_binding = _loader_binding(node, loaders, key_prefixes)
        if loader_binding is not None:
            cfg_file, key_prefix = loader_binding
            return cfg_file, [key_prefix] if key_prefix else []

        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            return None
        parent = _config_chain(func.value, bindings, loaders, key_prefixes)
        if parent is None:
            return None
        cfg_file, parts = parent
        key = (
            node.args[0].value
            if node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            else None
        )
        if key is None:
            return None
        return cfg_file, [*parts, key]

    if isinstance(node, ast.Subscript):
        key = _subscript_string_key(node)
        if key is None:
            return None
        parent = _config_chain(node.value, bindings, loaders, key_prefixes)
        if parent is None:
            return None
        cfg_file, parts = parent
        return cfg_file, [*parts, key]

    return None


# ---------------------------------------------------------------------------
# Binding collection (sequential walk so sub-bindings are visible downstream)
# ---------------------------------------------------------------------------


def _iter_control_flow_bodies(stmt: ast.stmt) -> Iterator[list[ast.stmt]]:
    """Yield child statement lists inside control-flow nodes (not def/class)."""
    if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        yield stmt.body
        if getattr(stmt, "orelse", None):
            yield stmt.orelse
    elif isinstance(stmt, ast.Try):
        yield stmt.body
        for handler in stmt.handlers:
            yield handler.body
        if stmt.orelse:
            yield stmt.orelse
        if stmt.finalbody:
            yield stmt.finalbody
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        yield stmt.body


def _try_assign_to_binding(
    value: ast.expr,
    bindings: _Bindings,
    loaders: Mapping[str, str],
    key_prefixes: Mapping[str, str],
) -> tuple[str, str] | None:
    """Return ``(config_file, key_prefix)`` if ``value`` creates a config binding."""
    if isinstance(value, ast.Call):
        loader_binding = _loader_binding(value, loaders, key_prefixes)
        if loader_binding is not None:
            return loader_binding
    # Sub-binding: var = cfg.get("key") or var = cfg["key"]
    result = _config_chain(value, bindings, loaders, key_prefixes)
    if result is not None:
        cfg_file, parts = result
        return cfg_file, ".".join(parts)
    return None


def _collect_bindings(
    stmts: list[ast.stmt],
    bindings: _Bindings,
    loaders: Mapping[str, str],
    key_prefixes: Mapping[str, str],
) -> None:
    """Populate ``bindings`` by walking ``stmts`` in order.

    Recurses into control-flow bodies but NOT into nested ``def``/``class``
    so that inner scopes cannot pollute the outer binding dict.
    """
    for stmt in stmts:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.expr):
            binding = _try_assign_to_binding(stmt.value, bindings, loaders, key_prefixes)
            if binding is not None:
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = binding
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            binding = _try_assign_to_binding(stmt.value, bindings, loaders, key_prefixes)
            if binding is not None and isinstance(stmt.target, ast.Name):
                bindings[stmt.target.id] = binding
        # Recurse into control-flow; stop at nested def/class boundaries
        for child_stmts in _iter_control_flow_bodies(stmt):
            _collect_bindings(child_stmts, bindings, loaders, key_prefixes)


# ---------------------------------------------------------------------------
# Edge emission
# ---------------------------------------------------------------------------


def _emit_if_config_access(
    node: ast.AST,
    bindings: _Bindings,
    edges: list[Edge],
    rel_path: str,
    owner: str | None,
    loaders: Mapping[str, str],
    key_prefixes: Mapping[str, str],
) -> None:
    """Emit a ``reads_config`` edge when ``node`` is a config key access."""
    if isinstance(node, ast.Call):
        loader_binding = _loader_binding(node, loaders, key_prefixes)
        if loader_binding is not None:
            cfg_file, key_prefix = loader_binding
            if key_prefix:
                src_kind, src = source_with_symbol(rel_path, owner)
                edges.append(
                    Edge(
                        src_kind=src_kind,
                        src=src,
                        dst_kind="config",
                        dst=f"{cfg_file}::{key_prefix}",
                        edge_type="reads_config",
                        lineno=node.lineno,
                        evidence=f"L{node.lineno}: {_call_name(node)}()",
                    )
                )
            return

    if isinstance(node, ast.Call):
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            return
        result = _config_chain(func.value, bindings, loaders, key_prefixes)
        if result is None:
            return
        cfg_file, parts = result
        if not (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return
        key: str = node.args[0].value
        full_path = ".".join([*parts, key])
        src_kind, src = source_with_symbol(rel_path, owner)
        edges.append(
            Edge(
                src_kind=src_kind,
                src=src,
                dst_kind="config",
                dst=f"{cfg_file}::{full_path}",
                edge_type="reads_config",
                lineno=node.lineno,
                evidence=f"L{node.lineno}: .get({key!r})",
            )
        )
        return

    if isinstance(node, ast.Subscript):
        key = _subscript_string_key(node)
        if key is None:
            return
        result = _config_chain(node.value, bindings, loaders, key_prefixes)
        if result is None:
            return
        cfg_file, parts = result
        full_path = ".".join([*parts, key])
        src_kind, src = source_with_symbol(rel_path, owner)
        lineno = getattr(node, "lineno", 0)
        edges.append(
            Edge(
                src_kind=src_kind,
                src=src,
                dst_kind="config",
                dst=f"{cfg_file}::{full_path}",
                edge_type="reads_config",
                lineno=lineno,
                evidence=f"L{lineno}: [{key!r}]",
            )
        )


# ---------------------------------------------------------------------------
# Scope-level driver
# ---------------------------------------------------------------------------


def _iter_scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield nodes within the current scope, excluding nested def/class bodies."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _iter_scope_nodes(child)


def _process_scope(
    stmts: list[ast.stmt],
    rel_path: str,
    owner: str | None,
    edges: list[Edge],
    owners_list: list,
    loaders: Mapping[str, str],
    key_prefixes: Mapping[str, str],
) -> None:
    """Collect bindings for ``stmts`` and emit edges for all config accesses."""
    bindings: _Bindings = {}
    _collect_bindings(stmts, bindings, loaders, key_prefixes)
    for stmt in stmts:
        for node in _iter_scope_nodes(stmt):
            lineno = getattr(node, "lineno", None)
            resolved_owner = owner_for_lineno(owners_list, lineno) if lineno else owner
            _emit_if_config_access(
                node, bindings, edges, rel_path, resolved_owner or owner, loaders, key_prefixes
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_config_edges_for_tree(
    *,
    rel_path: str,
    tree: ast.Module,
    loaders: Mapping[str, str] | None = None,
    key_prefixes: Mapping[str, str] | None = None,
) -> list[Edge]:
    """Return ``reads_config`` edges from a parsed Python AST.

    ``loaders`` maps config-loader function names to the config file path
    recorded as the edge target; ``key_prefixes`` maps wrapper-loader names
    to the key prefix they already traversed. Both default to empty.
    """
    loaders = dict(loaders or {})
    key_prefixes = dict(key_prefixes or {})
    owners_list = top_level_owners(tree)
    edges: list[Edge] = []

    module_level_stmts: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _process_scope(stmt.body, rel_path, stmt.name, edges, owners_list, loaders, key_prefixes)
        else:
            module_level_stmts.append(stmt)

    if module_level_stmts:
        _process_scope(module_level_stmts, rel_path, None, edges, owners_list, loaders, key_prefixes)

    return edges


def extract_config_edges(
    path: Path,
    rel_path: str,
    loaders: Mapping[str, str] | None = None,
    key_prefixes: Mapping[str, str] | None = None,
) -> list[Edge]:
    """Return ``reads_config`` edges for the Python file at ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        logger.warning("lineage/config: skipping %s: %s", path, exc)
        return []
    return extract_config_edges_for_tree(
        rel_path=rel_path, tree=tree, loaders=loaders, key_prefixes=key_prefixes
    )
