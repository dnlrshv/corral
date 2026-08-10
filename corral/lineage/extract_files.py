"""Detect ``reads_file`` / ``writes_file`` edges from common I/O calls.

This is deliberately a narrow allow-list: pandas / pyarrow read & write
helpers, plus builtin ``open(path, mode)``. We do not attempt to track
``shutil`` / ``os.replace`` / arbitrary file moves — those carry too little
intent to be useful at the lineage layer.

When the first/second positional arg of a recognised call is a constant or
all-constant f-string, we emit the corresponding edge with the path as the
``dst``. Dynamic paths (interpolated variables) are skipped because they
would only produce noise like ``"data/foo " " .parquet"``.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from .python_ast import (
    TopLevelOwner,
    call_attr_chain,
    flatten_string_node,
    owner_for_lineno,
    source_with_symbol,
    top_level_owners,
)
from .schema import Edge

logger = logging.getLogger(__name__)

# (chain-tail, edge_type, path_arg_index)
# chain-tail is the final attribute / function name. We do not require a
# specific receiver (``pd.read_parquet`` and ``pandas.read_parquet`` both
# count as ``read_parquet``).
_READ_FUNCTIONS: tuple[tuple[str, int], ...] = (
    ("read_parquet", 0),
    ("read_csv", 0),
    ("read_json", 0),
    ("read_table", 0),  # pyarrow.parquet.read_table / pyarrow.csv.read_csv path
    ("read_pickle", 0),
    ("read_text", 0),  # Path(...).read_text() — receiver carries the path, see below
    ("read_bytes", 0),
)

_WRITE_FUNCTIONS: tuple[tuple[str, int], ...] = (
    ("to_parquet", 0),
    ("to_csv", 0),
    ("to_json", 0),
    ("to_pickle", 0),
    ("write_table", 1),  # pq.write_table(table, path)
    ("write_text", 0),  # Path(...).write_text(...)
    ("write_bytes", 0),
)

_OPEN_READ_MODES = ("r", "rb", "rt")
_OPEN_WRITE_MODE_PREFIXES = ("w", "a", "x")

# Path conventions for the read_text / write_text / read_bytes / write_bytes
# family: those are receiver-carrying (``Path(p).read_text()``), so the
# allow-list above is a no-op for them. We special-case Path(...).method()
# below instead.
_PATH_RECEIVER_METHODS_READ = {"read_text", "read_bytes"}
_PATH_RECEIVER_METHODS_WRITE = {"write_text", "write_bytes"}


def _constant_string(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.JoinedStr):
        return _constant_joined_string(node)
    return flatten_string_node(node)


def _constant_joined_string(node: ast.JoinedStr) -> str | None:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif (
            isinstance(value, ast.FormattedValue)
            and value.conversion == -1
            and value.format_spec is None
            and isinstance(value.value, ast.Constant)
            and isinstance(value.value.value, str)
        ):
            parts.append(value.value.value)
        else:
            return None
    return "".join(parts)


def _constant_string_arg(call: ast.Call, index: int) -> tuple[bool, str | None]:
    arg = _positional(list(call.args), index)
    if arg is None:
        return False, None
    return True, _constant_string(arg)


def _constant_keyword(call: ast.Call, name: str) -> tuple[bool, str | None]:
    for kw in call.keywords:
        if kw.arg == name:
            return True, _constant_string(kw.value)
    return False, None


def _positional(args: list[ast.expr], index: int) -> ast.expr | None:
    if 0 <= index < len(args):
        return args[index]
    return None


def _path_arg_from_call(call: ast.Call, index: int) -> str | None:
    """Return the constant string at ``args[index]`` of ``call``, if any."""

    return _constant_string(_positional(list(call.args), index))


def _emit(
    *,
    edges: list[Edge],
    rel_path: str,
    owner: str | None,
    lineno: int,
    edge_type: str,
    dst: str,
    evidence: str,
) -> None:
    src_kind, src = source_with_symbol(rel_path, owner)
    edges.append(
        Edge(
            src_kind=src_kind,
            src=src,
            dst_kind="file",
            dst=dst,
            edge_type=edge_type,
            lineno=lineno,
            evidence=evidence,
        )
    )


def _handle_open_call(
    *,
    call: ast.Call,
    edges: list[Edge],
    rel_path: str,
    owner: str | None,
) -> None:
    """Handle ``open(path, mode)`` — defaulting to read mode."""

    path = _path_arg_from_call(call, 0)
    if path is None:
        return
    has_mode, mode_arg = _constant_string_arg(call, 1)
    if not has_mode:
        has_mode, mode_arg = _constant_keyword(call, "mode")
    if has_mode and mode_arg is None:
        return
    mode = (mode_arg or "r").lower()

    if mode in _OPEN_READ_MODES:
        edge_type = "reads_file"
    elif mode.startswith(_OPEN_WRITE_MODE_PREFIXES):
        edge_type = "writes_file"
    else:
        # Skip exotic modes like '+' updates — direction is ambiguous.
        return

    _emit(
        edges=edges,
        rel_path=rel_path,
        owner=owner,
        lineno=call.lineno,
        edge_type=edge_type,
        dst=path,
        evidence=f"L{call.lineno}: open({path!r}, {mode!r})",
    )


def _handle_path_receiver_method(
    *,
    call: ast.Call,
    method: str,
    edges: list[Edge],
    rel_path: str,
    owner: str | None,
) -> bool:
    """Catch ``Path("foo").read_text()`` / ``Path("foo").write_text(...)``.

    Returns True when a Path-receiver method consumed this call so the
    caller does not also process it via the function-name allow-list.
    """

    if method not in _PATH_RECEIVER_METHODS_READ and method not in _PATH_RECEIVER_METHODS_WRITE:
        return False
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    receiver = func.value
    if not (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "Path"
    ):
        return False
    path = _path_arg_from_call(receiver, 0)
    if path is None:
        return True  # we recognised the shape; just nothing to emit
    edge_type = "reads_file" if method in _PATH_RECEIVER_METHODS_READ else "writes_file"
    _emit(
        edges=edges,
        rel_path=rel_path,
        owner=owner,
        lineno=call.lineno,
        edge_type=edge_type,
        dst=path,
        evidence=f"L{call.lineno}: Path({path!r}).{method}(...)",
    )
    return True


def _maybe_emit_for_function(
    *,
    chain: tuple[str, ...],
    call: ast.Call,
    edges: list[Edge],
    rel_path: str,
    owner: str | None,
) -> None:
    if not chain:
        return
    tail = chain[-1]

    if _handle_path_receiver_method(
        call=call, method=tail, edges=edges, rel_path=rel_path, owner=owner
    ):
        return

    for name, idx in _READ_FUNCTIONS:
        if tail == name:
            path = _path_arg_from_call(call, idx)
            if path is None:
                return
            _emit(
                edges=edges,
                rel_path=rel_path,
                owner=owner,
                lineno=call.lineno,
                edge_type="reads_file",
                dst=path,
                evidence=f"L{call.lineno}: {'.'.join(chain)}({path!r})",
            )
            return

    for name, idx in _WRITE_FUNCTIONS:
        if tail == name:
            path = _path_arg_from_call(call, idx)
            if path is None:
                return
            _emit(
                edges=edges,
                rel_path=rel_path,
                owner=owner,
                lineno=call.lineno,
                edge_type="writes_file",
                dst=path,
                evidence=f"L{call.lineno}: {'.'.join(chain)}({path!r})",
            )
            return


def extract_file_edges_for_tree(
    *,
    rel_path: str,
    tree: ast.Module,
) -> list[Edge]:
    owners: list[TopLevelOwner] = top_level_owners(tree)
    edges: list[Edge] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        owner = owner_for_lineno(owners, node.lineno)
        chain = call_attr_chain(node)
        if chain == ("open",):
            _handle_open_call(call=node, edges=edges, rel_path=rel_path, owner=owner)
            continue
        _maybe_emit_for_function(
            chain=chain, call=node, edges=edges, rel_path=rel_path, owner=owner
        )
    return edges


def extract_file_edges(path: Path, rel_path: str) -> list[Edge]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        logger.warning("lineage: skipping %s: %s", path, exc)
        return []
    return extract_file_edges_for_tree(rel_path=rel_path, tree=tree)
