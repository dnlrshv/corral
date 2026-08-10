"""AST-based function call-graph edge extractor for ``code_map/edges.parquet``.

Detects intra-project ``calls`` edges (src function → dst function) by:

1. Building a repo-wide ``SymbolIndex`` from all scanned Python files.
2. Parsing each file's ``import`` / ``from … import`` statements to build a
   local name → resolved-symbol mapping.
3. Walking each top-level function's AST to find ``Call`` nodes, then
   resolving the callee through the import map and the same-file symbol table.

Only edges where the callee resolves to a project-local symbol are emitted;
stdlib and third-party calls are silently skipped.

The extractor is deliberately static and conservative:

* **deterministic** — same source inputs → same edges → byte-identical parquet,
* **portable** — no external process or network required,
* **scoped** — only intra-project calls with statically resolvable targets.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .python_ast import (
    call_attr_chain,
    owner_for_lineno,
    source_with_symbol,
    top_level_owners,
)
from .schema import Edge

# ---------------------------------------------------------------------------
# Symbol index
# ---------------------------------------------------------------------------


@dataclass
class SymbolIndex:
    """Repo-wide symbol lookup tables built from all scanned Python files."""

    # "dotted.module.SymbolName" → "rel/file.py:SymbolName"
    by_qualified: dict[str, str] = field(default_factory=dict)
    # "dotted.module" → "rel/file.py"
    by_module: dict[str, str] = field(default_factory=dict)
    # "rel/file.py" → frozenset of top-level symbol names defined there
    by_file: dict[str, frozenset[str]] = field(default_factory=dict)


def _dotted_module(rel_path: str) -> str:
    """Convert ``src/foo/_bar.py`` → ``src.foo._bar``."""
    mod = Path(rel_path).with_suffix("").as_posix().replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def build_symbol_index(root: Path, files: list[Path]) -> SymbolIndex:
    """Scan *files* to build a repo-wide symbol lookup index."""
    idx = SymbolIndex()
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except SyntaxError:
            continue

        rel = path.relative_to(root).as_posix()
        mod = _dotted_module(rel)
        idx.by_module[mod] = rel

        names: list[str] = []
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(stmt.name)
                idx.by_qualified[f"{mod}.{stmt.name}"] = f"{rel}:{stmt.name}"
        idx.by_file[rel] = frozenset(names)

    return idx


# ---------------------------------------------------------------------------
# Import map
# ---------------------------------------------------------------------------


def _relative_base_parts(rel_path: str, level: int) -> list[str]:
    """Return module parts that ``level`` leading dots resolve to.

    ``level=1`` means "same package", ``level=2`` means "parent package".
    """
    parts = Path(rel_path).with_suffix("").as_posix().replace("/", ".").split(".")
    if parts:
        parts = parts[:-1]
    drop = max(0, level - 1)
    return parts[: len(parts) - drop] if drop < len(parts) else []


def _bindings_from_import_node(
    node: ast.Import | ast.ImportFrom,
    rel_path: str,
    idx: SymbolIndex,
) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(node, ast.Import):
        for alias in node.names:
            dotted = alias.name
            local = alias.asname if alias.asname else alias.name.split(".")[0]
            mod_file = idx.by_module.get(dotted)
            if mod_file:
                result[local] = mod_file
        return result

    module = node.module or ""
    level = node.level or 0
    if level:
        base = _relative_base_parts(rel_path, level)
        module = ".".join(base + ([module] if module else []))

    for alias in node.names:
        if alias.name == "*":
            continue
        local = alias.asname or alias.name
        qualified = f"{module}.{alias.name}"
        if qualified in idx.by_qualified:
            result[local] = idx.by_qualified[qualified]
            continue
        sub_file = idx.by_module.get(qualified)
        if sub_file:
            result[local] = sub_file
            continue
        mod_file = idx.by_module.get(module)
        if mod_file and alias.name in idx.by_file.get(mod_file, frozenset()):
            result[local] = f"{mod_file}:{alias.name}"
    return result


def parse_local_import_map(tree: ast.Module, rel_path: str, idx: SymbolIndex) -> dict[str, str]:
    """Return module-scope ``{local_name: destination}`` imports.

    *destination* is one of:

    * ``"rel/file.py"``          — the import bound *name* to a module,
    * ``"rel/file.py:symbol"``   — the import bound *name* to a specific symbol.
    """
    result: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.update(_bindings_from_import_node(node, rel_path, idx))

    return result


class _ScopedImportCollector(ast.NodeVisitor):
    def __init__(self, rel_path: str, idx: SymbolIndex) -> None:
        self.rel_path = rel_path
        self.idx = idx
        self.bindings: list[tuple[int, dict[str, str]]] = []

    def visit_Import(self, node: ast.Import) -> None:
        self.bindings.append(
            (node.lineno, _bindings_from_import_node(node, self.rel_path, self.idx))
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.bindings.append(
            (node.lineno, _bindings_from_import_node(node, self.rel_path, self.idx))
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _scoped_import_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    idx: SymbolIndex,
) -> list[tuple[int, dict[str, str]]]:
    collector = _ScopedImportCollector(rel_path, idx)
    for stmt in node.body:
        collector.visit(stmt)
    return sorted(collector.bindings, key=lambda item: item[0])


def _imports_visible_at_line(
    module_imports: dict[str, str],
    scoped_bindings: list[tuple[int, dict[str, str]]],
    lineno: int,
) -> dict[str, str]:
    visible = dict(module_imports)
    for import_lineno, bindings in scoped_bindings:
        if import_lineno > lineno:
            break
        visible.update(bindings)
    return visible


# ---------------------------------------------------------------------------
# Call resolution
# ---------------------------------------------------------------------------


def _resolve_callee(
    chain: tuple[str, ...],
    rel_path: str,
    idx: SymbolIndex,
    import_map: dict[str, str],
) -> str | None:
    """Resolve a call chain to ``"rel/file.py:symbol"`` or ``None``.

    Returns ``None`` for unresolvable or external callees (silently skip).
    """
    if not chain:
        return None

    head = chain[0]

    # --- Simple call: func() ---
    if len(chain) == 1:
        if head in import_map:
            target = import_map[head]
            # Only return if it's a symbol reference ("file.py:sym"), not a bare module
            return target if ":" in target else None
        if head in idx.by_file.get(rel_path, frozenset()):
            return f"{rel_path}:{head}"
        return None

    # --- Attribute call: obj.method() or pkg.func() ---
    if head not in import_map:
        return None
    target = import_map[head]
    if ":" in target:
        # target is a symbol ("file.py:cls"); can't resolve .attr() without type info
        return None
    # target is a module file; look up the last chain segment as a symbol there
    attr = chain[-1]
    if attr in idx.by_file.get(target, frozenset()):
        return f"{target}:{attr}"
    return None


# ---------------------------------------------------------------------------
# Edge extraction
# ---------------------------------------------------------------------------


def extract_call_edges_for_tree(
    rel_path: str,
    tree: ast.Module,
    idx: SymbolIndex,
    import_map: dict[str, str],
) -> list[Edge]:
    """Return call edges for all resolved intra-project calls in *tree*."""
    owners = top_level_owners(tree)
    edges: list[Edge] = []
    seen: set[tuple[str, str, int]] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        owner = owner_for_lineno(owners, node.lineno)
        src_kind, src = source_with_symbol(rel_path, owner)
        scoped_bindings = _scoped_import_bindings(node, rel_path, idx)

        for subnode in ast.walk(node):
            if not isinstance(subnode, ast.Call):
                continue
            chain = call_attr_chain(subnode)
            visible_imports = _imports_visible_at_line(import_map, scoped_bindings, subnode.lineno)
            resolved = _resolve_callee(chain, rel_path, idx, visible_imports)
            if resolved is None:
                continue
            key = (src, resolved, subnode.lineno)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                Edge(
                    src_kind=src_kind,
                    src=src,
                    dst_kind="symbol",
                    dst=resolved,
                    edge_type="calls",
                    lineno=subnode.lineno,
                    evidence=f"{'.'.join(chain)}()",
                )
            )

    return edges


def extract_call_edges(path: Path, rel_path: str, idx: SymbolIndex) -> list[Edge]:
    """Parse *path* and return its call edges using the shared *idx*."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except SyntaxError:
        return []
    import_map = parse_local_import_map(tree, rel_path, idx)
    return extract_call_edges_for_tree(rel_path, tree, idx, import_map)
