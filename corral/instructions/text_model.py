"""Position-preserving Markdown views; no commands or links are evaluated."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re

import yaml


@dataclass(frozen=True)
class CodeSpan:
    start: int
    end: int
    text: str
    info: str = ''
    fenced: bool = False


class TextModel:
    def __init__(self, text: str, *, frontmatter: bool = True):
        self.original = text.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
        text = self.original
        self.line_starts = [0] + [m.end() for m in re.finditer('\n', text)]
        self.frontmatter_end = 0
        self.frontmatter = None
        self.frontmatter_start = 0
        self.code_spans: list[CodeSpan] = []
        self.links: list[tuple[int, int, str]] = []
        self.comments: list[tuple[int, int]] = []
        prose = list(text)
        code = ['\n' if c == '\n' else ' ' for c in text]
        self.nonfenced = list(text)

        def mask(start, end):
            for i in range(start, end):
                if text[i] != '\n':
                    prose[i] = ' '

        lines = text.splitlines(keepends=True)
        if frontmatter and lines and lines[0].rstrip('\n') == '---':
            end = len(lines[0])
            for line in lines[1:]:
                end += len(line)
                if line.rstrip('\n') == '---':
                    break
            self.frontmatter_end = end
            self.frontmatter_start = len(lines[0])
            try:
                self.frontmatter = yaml.safe_load(text[len(lines[0]):end].rsplit('---', 1)[0])
            except (yaml.YAMLError, ValueError, RecursionError):
                pass
            mask(0, end)
            for i in range(end):
                if text[i] != '\n':
                    self.nonfenced[i] = ' '
        offset = 0
        fence = None
        for line in lines:
            start, end = offset, offset + len(line)
            offset = end
            if start < self.frontmatter_end:
                continue
            if fence:
                mask(start, end)
                for i in range(start, end):
                    if text[i] != '\n':
                        self.nonfenced[i] = ' '
                if re.match(r'^\s*' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}(?!' + re.escape(fence[0]) + ')', line):
                    self.code_spans.append(CodeSpan(fence[2], start, text[fence[2]:start], fence[3], True))
                    fence = None
                else:
                    code[start:end] = text[start:end]
            else:
                match = re.match(r'^\s*(`{3,}|~{3,})(.*)', line)
                if match:
                    fence = (match[1][0], len(match[1]), end, match[2].strip().split(' ')[0])
                    mask(start, end)
                    for i in range(start, end):
                        if text[i] != '\n':
                            self.nonfenced[i] = ' '
        if fence:
            self.code_spans.append(CodeSpan(fence[2], len(text), text[fence[2]:], fence[3], True))
        # Inline runs close only at a run of exactly the same length.
        runs = list(re.finditer(r'`+', ''.join(prose)))
        next_run = {}
        partners = {}
        for index in range(len(runs) - 1, -1, -1):
            size = len(runs[index][0])
            partners[index] = next_run.get(size)
            next_run[size] = index
        index = 0
        while index < len(runs):
            other = partners[index]
            if other is None:
                index += 1
                continue
            opening, closing = runs[index], runs[other]
            self.code_spans.append(CodeSpan(opening.end(), closing.start(), text[opening.end():closing.start()]))
            code[opening.end():closing.start()] = text[opening.end():closing.start()]
            mask(opening.start(), closing.end())
            index = other + 1
        for match in re.finditer(r'<!--.*?(?:-->|\Z)', ''.join(prose), re.S):
            self.comments.append(match.span())
            mask(*match.span())
        for match in re.finditer(r'(?<!!)\[[^\[\]\n]*\]\((<[^>\n]+>|[^)\n]*)\)', ''.join(prose)):
            dest = match[1]
            self.links.append((match.start(1), match.end(1), dest.strip().strip('<>')))
            mask(match.start(1), match.end(1))
        for match in re.finditer(r'<https?://[^>\n]*>|https?://[^\s<>]+', ''.join(prose)):
            mask(*match.span())
        self.prose = ''.join(prose)
        self.code = ''.join(code)
        self.nonfenced = ''.join(self.nonfenced)

    def position(self, offset: int) -> tuple[int, int]:
        line = bisect_right(self.line_starts, offset)
        return line, offset - self.line_starts[line - 1] + 1

    def paragraphs(self, view: str | None = None):
        view = self.prose if view is None else view
        start = None
        offset = 0
        for line in view.splitlines(keepends=True):
            special = re.match(r'^\s*(?:[-*+] |\d+[.)] |#{1,6} |\|)', line)
            if not line.strip() or special:
                if start is not None:
                    yield start, offset, view[start:offset]
                    start = None
                if special:
                    yield offset, offset + len(line), line
            elif start is None:
                start = offset
            offset += len(line)
        if start is not None:
            yield start, len(view), view[start:]

    @staticmethod
    def sentences(text: str):
        start = 0
        for match in re.finditer(r'[.!?](?:\s+(?=[A-Z])|\s*$)', text):
            yield start, match.end(), text[start:match.end()]
            start = match.end()
        if start < len(text):
            yield start, len(text), text[start:]
