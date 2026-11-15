"""Byte-preserving, compare-before-replace writes for instruction files."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile


class ChangedDuringSync(OSError):
    pass


@dataclass(frozen=True)
class TextFile:
    raw: bytes
    text: str
    bom: bool
    newline: str

    def encode(self, text: str) -> bytes:
        return (b'\xef\xbb\xbf' if self.bom else b'') + text.encode('utf-8')


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def read_text_preserving(path: Path) -> TextFile:
    raw = read_bytes(path)
    if raw is None:
        return TextFile(b'', '', False, '\n')
    bom = raw.startswith(b'\xef\xbb\xbf')
    text = raw.decode('utf-8-sig' if bom else 'utf-8')
    endings = text.count('\n')
    newline = '\r\n' if endings and 2 * text.count('\r\n') >= endings else '\n'
    return TextFile(raw, text, bom, newline)


def _new_file_mode():
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


_NEW_FILE_MODE = None


def write_bytes_atomic(path: Path, data: bytes, *, expected_old: bytes | None, mode: int | None = None):
    global _NEW_FILE_MODE
    if _NEW_FILE_MODE is None:
        _NEW_FILE_MODE = _new_file_mode()
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode) if expected_old is not None else _NEW_FILE_MODE
        except OSError as exc:
            raise ChangedDuringSync('changed during sync; re-run') from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        try:
            current = read_bytes(path)
        except OSError as exc:
            raise ChangedDuringSync('changed during sync; re-run') from exc
        if current != expected_old:
            raise ChangedDuringSync('changed during sync; re-run')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text_atomic(path: Path, text: str, *, expected_old: bytes | None):
    write_bytes_atomic(path, text.encode('utf-8'), expected_old=expected_old)


def copy_file_atomic(src: Path, dst: Path):
    write_bytes_atomic(dst, src.read_bytes(), expected_old=read_bytes(dst), mode=stat.S_IMODE(src.stat().st_mode))
