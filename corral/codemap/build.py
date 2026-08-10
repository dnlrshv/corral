"""Build deterministic Python import and symbol maps as parquet artifacts.

Usage:
    corral codemap build --root . --output-dir code_map/
"""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

#: Repo-root-relative directories that are never scanned. Projects override
#: this via ``codemap.skip_dirs`` in ``corral.yaml``.
SKIP_DIRS = (
    ".venv",
    "data",
    "tests/fixtures",
    ".claude/worktrees",
)
CACHE_DIR_NAME = ".cache"
CACHE_KEEP_COUNT = 5
CACHE_TMP_PREFIX = ".tmp-"
CACHE_TMP_MAX_AGE_SECONDS = 3600
OUTPUT_FILENAMES = ("imports.parquet", "symbols.parquet")

IMPORTS_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("target_module", pa.string()),
        ("target_symbol", pa.string()),
        ("is_relative", pa.bool_()),
        ("lineno", pa.int64()),
    ]
)

SYMBOLS_SCHEMA = pa.schema(
    [
        ("file", pa.string()),
        ("symbol", pa.string()),
        ("kind", pa.string()),
        ("lineno", pa.int64()),
        ("is_public", pa.bool_()),
    ]
)


@dataclass(frozen=True)
class ImportEdge:
    source_file: str
    target_module: str
    target_symbol: str | None
    is_relative: bool
    lineno: int


@dataclass(frozen=True)
class SymbolEntry:
    file: str
    symbol: str
    kind: str
    lineno: int
    is_public: bool


def _normalized_relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_skipped(rel_path: str, skip_dirs: Iterable[str] = SKIP_DIRS) -> bool:
    return any(rel_path == skipped or rel_path.startswith(f"{skipped}/") for skipped in skip_dirs)


def _normalized_scan_dir(scan_dir: str) -> str:
    parts = [part for part in scan_dir.replace("\\", "/").split("/") if part not in ("", ".")]
    return "/".join(parts)


def _under_scan_dirs(rel_path: str, scan_dirs: Iterable[str]) -> bool:
    for scan_dir in scan_dirs:
        normalized = _normalized_scan_dir(scan_dir)
        if not normalized:
            return True
        if rel_path == normalized or rel_path.startswith(f"{normalized}/"):
            return True
    return False


def _tracked_python_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "*.py"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [root / line for line in result.stdout.splitlines() if line]


def iter_python_files(
    root: Path,
    *,
    scan_dirs: Iterable[str] = (".",),
    skip_dirs: Iterable[str] = SKIP_DIRS,
) -> list[Path]:
    """Return sorted Python files under root, preferring git-tracked files when available."""
    # Materialize once: both filters below consume the inputs per file, so
    # a generator passed in would be exhausted after the first iteration.
    scan_dirs = tuple(scan_dirs)
    skip_dirs = tuple(skip_dirs)
    candidates = _tracked_python_files(root)
    if not candidates:
        candidates = list(root.rglob("*.py"))

    files: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        rel_path = _normalized_relative_path(path, root)
        if rel_path in seen:
            continue
        if _is_skipped(rel_path, skip_dirs):
            continue
        if not _under_scan_dirs(rel_path, scan_dirs):
            continue
        seen.add(rel_path)
        files.append(path)

    return sorted(files, key=lambda p: _normalized_relative_path(p, root))


def _literal_string_set(node: ast.AST) -> set[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None

    values: set[str] = set()
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.add(item.value)
    return values


def _module_all(tree: ast.Module) -> set[str] | None:
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in stmt.targets
        ):
            continue
        values = _literal_string_set(stmt.value)
        if values is not None:
            return values
    return None


def _is_public_symbol(name: str, module_all: set[str] | None) -> bool:
    if module_all is not None:
        return name in module_all
    return not name.startswith("_")


def _symbol_kind(stmt: ast.AST) -> str | None:
    if isinstance(stmt, ast.AsyncFunctionDef):
        return "async_function"
    if isinstance(stmt, ast.FunctionDef):
        return "function"
    if isinstance(stmt, ast.ClassDef):
        return "class"
    return None


def _import_module_name(node: ast.ImportFrom) -> str:
    prefix = "." * node.level
    return f"{prefix}{node.module or ''}"


def _imports_for_node(source_file: str, node: ast.AST) -> Iterable[ImportEdge]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield ImportEdge(
                source_file=source_file,
                target_module=alias.name,
                target_symbol=None,
                is_relative=False,
                lineno=node.lineno,
            )
    elif isinstance(node, ast.ImportFrom):
        module_name = _import_module_name(node)
        for alias in node.names:
            yield ImportEdge(
                source_file=source_file,
                target_module=module_name,
                target_symbol=alias.name,
                is_relative=node.level > 0,
                lineno=node.lineno,
            )


def parse_python_file(path: Path, root: Path) -> tuple[list[ImportEdge], list[SymbolEntry]]:
    source_file = _normalized_relative_path(path, root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"warning: skipping unparsable Python file {source_file}: {exc}\n")
        return [], []

    module_all = _module_all(tree)

    imports = [edge for node in ast.walk(tree) for edge in _imports_for_node(source_file, node)]

    symbols: list[SymbolEntry] = []
    for stmt in tree.body:
        kind = _symbol_kind(stmt)
        if kind is None:
            continue
        name = stmt.name
        symbols.append(
            SymbolEntry(
                file=source_file,
                symbol=name,
                kind=kind,
                lineno=stmt.lineno,
                is_public=_is_public_symbol(name, module_all),
            )
        )

    return imports, symbols


def build_code_map(
    root: Path,
    *,
    scan_dirs: Iterable[str] = (".",),
    skip_dirs: Iterable[str] = SKIP_DIRS,
) -> tuple[list[ImportEdge], list[SymbolEntry]]:
    scan_dirs = tuple(scan_dirs)
    skip_dirs = tuple(skip_dirs)
    root = root.resolve()
    imports: list[ImportEdge] = []
    symbols: list[SymbolEntry] = []
    for path in iter_python_files(root, scan_dirs=scan_dirs, skip_dirs=skip_dirs):
        file_imports, file_symbols = parse_python_file(path, root)
        imports.extend(file_imports)
        symbols.extend(file_symbols)

    return (
        sorted(
            imports,
            key=lambda edge: tuple("" if item is None else item for item in edge.__dict__.values()),
        ),
        sorted(symbols, key=lambda entry: tuple(entry.__dict__.values())),
    )


def _table_from_dataclasses(rows: list[object], schema: pa.Schema) -> pa.Table:
    columns = {field.name: [getattr(row, field.name) for row in rows] for field in schema}
    return pa.table(columns, schema=schema)


def write_code_map(imports: list[ImportEdge], symbols: list[SymbolEntry], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        _table_from_dataclasses(imports, IMPORTS_SCHEMA),
        output_dir / "imports.parquet",
        compression="zstd",
        use_dictionary=False,
        write_statistics=False,
    )
    pq.write_table(
        _table_from_dataclasses(symbols, SYMBOLS_SCHEMA),
        output_dir / "symbols.parquet",
        compression="zstd",
        use_dictionary=False,
        write_statistics=False,
    )


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        text=True,
    )


def _git_tree_sha(root: Path) -> str | None:
    result = _run_git(root, ["rev-parse", "HEAD^{tree}"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_tree_is_clean(root: Path) -> bool:
    return (
        _run_git(root, ["diff", "--quiet"]).returncode == 0
        and _run_git(root, ["diff", "--cached", "--quiet"]).returncode == 0
    )


def _copy_code_map_files(source_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in OUTPUT_FILENAMES:
        shutil.copy2(source_dir / filename, output_dir / filename)


def _cache_entry_is_complete(cache_entry: Path) -> bool:
    return all((cache_entry / filename).is_file() for filename in OUTPUT_FILENAMES)


def _write_cache_entry(
    imports: list[ImportEdge],
    symbols: list[SymbolEntry],
    cache_entry: Path,
) -> bool:
    """Promote the build outputs into the per-tree-sha cache entry.

    Returns True when ``cache_entry`` ends the call as a complete entry the
    caller can copy from, False when a concurrent writer left an incomplete
    entry in place (caller should fall back to writing outputs directly).
    """
    cache_root = cache_entry.parent
    cache_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(prefix=f"{CACHE_TMP_PREFIX}{cache_entry.name}-", dir=cache_root)
    )
    try:
        write_code_map(imports, symbols, temp_dir)
        if cache_entry.exists() and not _cache_entry_is_complete(cache_entry):
            shutil.rmtree(cache_entry)
        try:
            temp_dir.replace(cache_entry)
        except OSError as exc:
            # Concurrent writer published cache_entry between our existence
            # check and the rename; rename(2) refuses to overwrite a non-empty
            # directory (EEXIST / ENOTEMPTY).
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            shutil.rmtree(temp_dir, ignore_errors=True)
            return _cache_entry_is_complete(cache_entry)
        return True
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _prune_stale_temp_dirs(
    cache_root: Path, max_age_seconds: int = CACHE_TMP_MAX_AGE_SECONDS
) -> None:
    if not cache_root.is_dir():
        return
    cutoff = time.time() - max_age_seconds
    for path in cache_root.iterdir():
        if not path.is_dir() or not path.name.startswith(CACHE_TMP_PREFIX):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def _prune_cache(cache_root: Path, keep_count: int = CACHE_KEEP_COUNT) -> None:
    if not cache_root.is_dir():
        return

    entries = sorted(
        (path for path in cache_root.iterdir() if path.is_dir() and not path.name.startswith(".")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale_entry in entries[keep_count:]:
        shutil.rmtree(stale_entry)
    _prune_stale_temp_dirs(cache_root)


def _scope_cache_key(scan_dirs: tuple[str, ...], skip_dirs: tuple[str, ...]) -> str:
    """Stable hash of the effective scan scope.

    The cache key must reflect what was actually scanned: two builds of the
    same tree with different ``scan_dirs`` / ``skip_dirs`` produce different
    parquets and must not reuse each other's cache entries. Scan dirs are
    normalized the same way the file filter does so equivalent spellings
    share an entry; order and duplicates do not change the result.
    """
    payload = json.dumps(
        {
            "scan_dirs": sorted({_normalized_scan_dir(d) for d in scan_dirs}),
            "skip_dirs": sorted(set(skip_dirs)),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_code_map_with_cache(
    root: Path,
    output_dir: Path,
    *,
    use_cache: bool = True,
    scan_dirs: Iterable[str] = (".",),
    skip_dirs: Iterable[str] = SKIP_DIRS,
) -> None:
    scan_dirs = tuple(scan_dirs)
    skip_dirs = tuple(skip_dirs)
    root = root.resolve()
    output_dir = output_dir.resolve()
    tree_sha = _git_tree_sha(root)
    clean_tree = tree_sha is not None and _git_tree_is_clean(root)
    cache_root = output_dir / CACHE_DIR_NAME
    scope_key = _scope_cache_key(scan_dirs, skip_dirs)

    if use_cache and clean_tree and tree_sha is not None:
        cache_entry = cache_root / f"{tree_sha}-{scope_key}"
        if _cache_entry_is_complete(cache_entry):
            _copy_code_map_files(cache_entry, output_dir)
            cache_entry.touch()
            _prune_cache(cache_root)
            return

        imports, symbols = build_code_map(root, scan_dirs=scan_dirs, skip_dirs=skip_dirs)
        if _write_cache_entry(imports, symbols, cache_entry):
            _copy_code_map_files(cache_entry, output_dir)
        else:
            write_code_map(imports, symbols, output_dir)
        _prune_cache(cache_root)
        return

    imports, symbols = build_code_map(root, scan_dirs=scan_dirs, skip_dirs=skip_dirs)
    write_code_map(imports, symbols, output_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="Repository or project root to scan"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory that receives imports.parquet and symbols.parquet",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force a direct rebuild without reading or writing the per-tree-sha cache",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    build_code_map_with_cache(args.root, args.output_dir, use_cache=not args.no_cache)


if __name__ == "__main__":
    main()
