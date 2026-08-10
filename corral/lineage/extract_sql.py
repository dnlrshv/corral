"""Detect ``reads_table`` / ``writes_table`` edges from SQL string literals.

We do not parse SQL: real query strings are often templated with f-strings,
and full grammars are heavy. Instead we scan every string literal that
*looks* like SQL (contains at least one keyword anchor) with a small set of
anchored regexes for the verbs we care about.

The patterns are deliberately conservative: each anchor must be word-bounded
and followed by an identifier-shaped table name. ``DELETE FROM <table>`` is
classified as a write (the row goes away); the read-side scan therefore
ignores any ``FROM`` that is part of a ``DELETE FROM``.

Connection-helper calls need no special handling — the SQL that the caller
eventually sends through such a connection still shows up as a string
literal in the same function and is captured by this extractor.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from .python_ast import (
    TopLevelOwner,
    flatten_string_node,
    owner_for_lineno,
    source_with_symbol,
    top_level_owners,
)
from .schema import Edge

logger = logging.getLogger(__name__)

# Identifier shape used after every verb anchor. Allows schema-qualified
# names like ``main.orders``; we keep the qualifier in the output so a
# reader can disambiguate cross-schema hits.
_IDENT = r"([A-Za-z_][\w\.]*)"

_WRITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("insert", re.compile(rf"\bINSERT\s+(?:OR\s+(?:REPLACE|IGNORE)\s+)?INTO\s+{_IDENT}", re.I)),
    ("replace", re.compile(rf"\bREPLACE\s+INTO\s+{_IDENT}", re.I)),
    ("update", re.compile(rf"\bUPDATE\s+{_IDENT}", re.I)),
    (
        "create",
        re.compile(
            rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}",
            re.I,
        ),
    ),
    ("delete", re.compile(rf"\bDELETE\s+FROM\s+{_IDENT}", re.I)),
    ("copy", re.compile(rf"\bCOPY\s+(?:INTO\s+)?{_IDENT}", re.I)),
    ("drop", re.compile(rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}", re.I)),
    ("truncate", re.compile(rf"\bTRUNCATE\s+(?:TABLE\s+)?{_IDENT}", re.I)),
)

_DELETE_FROM_PATTERN = re.compile(rf"\bDELETE\s+FROM\s+{_IDENT}", re.I)

_READ_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("from", re.compile(rf"\bFROM\s+{_IDENT}", re.I)),
    (
        "join",
        re.compile(rf"\b(?:INNER|LEFT|RIGHT|FULL|CROSS)?\s*(?:OUTER\s+)?JOIN\s+{_IDENT}", re.I),
    ),
)

# Tokens that should never be treated as table names even though the regex
# would otherwise grab them. Keeps noise out of edges.parquet.
_RESERVED: frozenset[str] = frozenset(
    {
        "select",
        "where",
        "from",
        "join",
        "on",
        "using",
        "and",
        "or",
        "not",
        "in",
        "as",
        "is",
        "null",
        "true",
        "false",
        "if",
        "exists",
        "values",
        "into",
        "table",
        "set",
        "by",
        "order",
        "group",
        "having",
        "limit",
        "offset",
        "with",
        "case",
        "when",
        "then",
        "else",
        "end",
        "distinct",
        "all",
        "any",
        "some",
        "left",
        "right",
        "inner",
        "outer",
        "cross",
        "union",
        "intersect",
        "except",
        "natural",
        "lateral",
        "asc",
        "desc",
        "between",
        "like",
        "ilike",
        "similar",
        "regexp",
        "match",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "transaction",
        "create",
        "alter",
        "drop",
        "delete",
        "insert",
        "update",
        "replace",
        "merge",
        "copy",
        "load",
        "export",
        "import",
        "primary",
        "key",
        "foreign",
        "references",
        "constraint",
        "unique",
        "check",
        "default",
    }
)

# Keyword anchors that mark a string as "SQL-ish enough to scan". Matching
# any of these is required before we run the verb regexes — keeps us from
# scanning every comment/path string in the repo.
_SQL_ANCHORS: tuple[str, ...] = (
    "SELECT ",
    "INSERT ",
    "UPDATE ",
    "DELETE ",
    "CREATE ",
    "REPLACE ",
    "COPY ",
    "DROP ",
    "TRUNCATE ",
    "MERGE ",
    " FROM ",
    " JOIN ",
)


def _looks_like_sql(text: str) -> bool:
    upper = text.upper()
    return any(anchor in upper for anchor in _SQL_ANCHORS)


def _is_table_name(name: str) -> bool:
    return name.lower() not in _RESERVED


def _evidence_snippet(text: str, lineno: int, max_len: int = 120) -> str:
    """One-line snippet for the ``evidence`` column."""

    collapsed = " ".join(text.split())
    if len(collapsed) > max_len:
        collapsed = collapsed[: max_len - 1].rstrip() + "…"
    return f"L{lineno}: {collapsed}"


def _scan_string_for_writes(text: str) -> list[tuple[str, str]]:
    """Return ``[(verb, table_name), ...]`` for write patterns in ``text``."""

    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for verb, pattern in _WRITE_PATTERNS:
        for match in pattern.finditer(text):
            table = match.group(1)
            if not _is_table_name(table):
                continue
            key = (verb, table)
            if key in seen:
                continue
            seen.add(key)
            hits.append(key)
    return hits


def _scan_string_for_reads(text: str) -> list[str]:
    """Return read-side table names, with ``DELETE FROM`` consumed first."""

    masked = _DELETE_FROM_PATTERN.sub(" ", text)
    tables: list[str] = []
    seen: set[str] = set()
    for _verb, pattern in _READ_PATTERNS:
        for match in pattern.finditer(masked):
            table = match.group(1)
            if not _is_table_name(table):
                continue
            if table in seen:
                continue
            seen.add(table)
            tables.append(table)
    return tables


def _iter_string_nodes(tree: ast.Module) -> list[ast.AST]:
    """Yield AST nodes whose flattened value we want to scan for SQL.

    Module/function/class docstrings are skipped — they often contain example
    snippets that are not actually executed against any database.
    """

    docstring_nodes: set[int] = set()
    for parent in ast.walk(tree):
        if not isinstance(
            parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(parent, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_nodes.add(id(first.value))

    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and id(node) not in docstring_nodes
    ]


def _string_nodes_with_text(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    out: list[tuple[ast.AST, str]] = []
    for node in _iter_string_nodes(tree):
        text = flatten_string_node(node)
        if text is None or not _looks_like_sql(text):
            continue
        out.append((node, text))
    return out


def extract_sql_edges_for_tree(
    *,
    rel_path: str,
    tree: ast.Module,
) -> list[Edge]:
    """Return SQL-derived edges for one parsed module."""

    owners: list[TopLevelOwner] = top_level_owners(tree)
    edges: list[Edge] = []

    for node, text in _string_nodes_with_text(tree):
        lineno = getattr(node, "lineno", 0) or 0
        owner = owner_for_lineno(owners, lineno)
        src_kind, src = source_with_symbol(rel_path, owner)
        evidence = _evidence_snippet(text, lineno)

        for _verb, table in _scan_string_for_writes(text):
            edges.append(
                Edge(
                    src_kind=src_kind,
                    src=src,
                    dst_kind="table",
                    dst=table,
                    edge_type="writes_table",
                    lineno=lineno,
                    evidence=evidence,
                )
            )

        for table in _scan_string_for_reads(text):
            edges.append(
                Edge(
                    src_kind=src_kind,
                    src=src,
                    dst_kind="table",
                    dst=table,
                    edge_type="reads_table",
                    lineno=lineno,
                    evidence=evidence,
                )
            )

    return edges


def extract_sql_edges(path: Path, rel_path: str) -> list[Edge]:
    """Parse ``path`` and return its SQL-derived edges. Skips unparsable files."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        logger.warning("lineage: skipping %s: %s", path, exc)
        return []
    return extract_sql_edges_for_tree(rel_path=rel_path, tree=tree)
