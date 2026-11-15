"""Managed Markdown blocks with normalized-body ownership digests."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


class MarkerError(ValueError):
    pass


@dataclass(frozen=True)
class BlockParse:
    start: int
    end: int
    start_line: int
    end_line: int
    digest: str
    body: str


def normalize(text: str) -> str:
    return text.removeprefix('\ufeff').replace('\r\n', '\n')


def body_digest(body: str) -> str:
    return hashlib.sha256(body.replace('\r\n', '\n').encode('utf-8')).hexdigest()[:12]


def render_block(name: str, body: str) -> str:
    body = body.replace('\r\n', '\n').rstrip('\n') + '\n'
    return f'<!-- corral:{name}:begin sha256={body_digest(body)} -->\n{body}<!-- corral:{name}:end -->\n'


def parse_block(text: str, name: str) -> BlockParse | None:
    begin = re.compile(r'<!-- corral:' + re.escape(name) + r':begin sha256=([0-9a-f]{12}) -->')
    end = f'<!-- corral:{name}:end -->'
    opened = None
    parsed = None
    offset = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.strip()
        if f'corral:{name}' in line:
            match = begin.fullmatch(stripped)
            if match:
                if opened is not None or parsed is not None:
                    raise MarkerError(f'second begin marker at line {index + 1}')
                opened = (offset, offset + len(line), index, match[1])
            elif stripped == end:
                if opened is None or parsed is not None:
                    raise MarkerError(f'end marker without begin at line {index + 1}')
                start, body_start, start_line, digest = opened
                parsed = BlockParse(start, offset + len(line), start_line, index, digest, text[body_start:offset])
                opened = None
            else:
                raise MarkerError(f'malformed marker at line {index + 1}')
        offset += len(line)
    if opened is not None:
        raise MarkerError(f'begin marker without end at line {opened[2] + 1}')
    return parsed
