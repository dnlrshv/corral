"""Reviewed deterministic retrieval-replay corpus model and loader."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KINDS = ("pr", "issue")


@dataclass(frozen=True)
class CorpusCase:
    number: int
    kind: str
    title: str
    task_text: str
    touched_paths: tuple[str, ...]
    expected_loads: tuple[str, ...]
    forbidden_loads: tuple[str, ...]
    max_bundle_tokens: int
    tier: str
    notes: str = ""

    @property
    def ref(self) -> str:
        return f"{self.kind}#{self.number}"


@dataclass(frozen=True)
class Corpus:
    profile: str
    generated_on: str
    source_repo: str
    cases: tuple[CorpusCase, ...]
    stratification: dict[str, Any] = field(default_factory=dict)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_corpus(corpus_path: Path) -> Corpus:
    raw = yaml.safe_load(corpus_path.read_text(encoding="utf-8")) or {}
    _require(isinstance(raw, dict), "corpus root must be a mapping")
    _require(isinstance(raw.get("cases"), list), "corpus needs a `cases` list")
    cases: list[CorpusCase] = []
    seen: set[tuple[str, int]] = set()
    for index, item in enumerate(raw["cases"]):
        location = f"cases[{index}]"
        _require(isinstance(item, dict), f"{location}: each case must be a mapping")
        for key in (
            "number",
            "kind",
            "title",
            "task_text",
            "touched_paths",
            "expected_loads",
            "forbidden_loads",
            "max_bundle_tokens",
            "tier",
        ):
            _require(key in item, f"{location}: missing required key {key!r}")
        _require(isinstance(item["number"], int), f"{location}: number must be an int")
        _require(item["kind"] in KINDS, f"{location}: kind must be one of {KINDS}")
        _require(
            isinstance(item["tier"], str) and bool(item["tier"]),
            f"{location}: tier must be a non-empty string",
        )
        _require(
            isinstance(item["max_bundle_tokens"], int)
            and not isinstance(item["max_bundle_tokens"], bool)
            and item["max_bundle_tokens"] > 0,
            f"{location}: max_bundle_tokens must be a positive int",
        )
        for key in ("touched_paths", "expected_loads", "forbidden_loads"):
            _require(
                isinstance(item[key], list)
                and all(isinstance(value, str) for value in item[key]),
                f"{location}: {key} must be a list of strings",
            )
        expected = set(item["expected_loads"])
        forbidden = set(item["forbidden_loads"])
        overlap = expected & forbidden
        _require(
            not overlap,
            f"{location}: paths in both expected and forbidden: {sorted(overlap)}",
        )
        identity = (item["kind"], item["number"])
        _require(identity not in seen, f"{location}: duplicate case {item['kind']}#{item['number']}")
        seen.add(identity)
        cases.append(
            CorpusCase(
                number=item["number"],
                kind=item["kind"],
                title=item["title"],
                task_text=item["task_text"],
                touched_paths=tuple(item["touched_paths"]),
                expected_loads=tuple(item["expected_loads"]),
                forbidden_loads=tuple(item["forbidden_loads"]),
                max_bundle_tokens=item["max_bundle_tokens"],
                tier=item["tier"],
                notes=item.get("notes", ""),
            )
        )
    _require(bool(cases), "corpus has no cases")
    return Corpus(
        profile=raw.get("profile", "default"),
        generated_on=raw.get("generated_on", ""),
        source_repo=raw.get("source_repo", ""),
        cases=tuple(cases),
        stratification=dict(raw.get("stratification", {})),
    )
