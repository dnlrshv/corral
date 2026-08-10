"""Validated model-seat registry loaded from ``seats.yaml``.

The registry deliberately contains only invocation facts.  Provider labels are
free-form identities used to prevent a drafter from verifying its own work;
there are no vendor aliases, budgets, pacing rules, or implicit credentials.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

SCHEMA_VERSION = 1
SUPPORTED_ADAPTERS = frozenset(
    {"anthropic-sdk", "openai-compatible-endpoint", "shell-command"}
)
SUPPORTED_OPENAI_PROTOCOLS = frozenset({"chat-completions", "responses"})


class SeatRegistryError(ValueError):
    """A ``seats.yaml`` document does not satisfy the seat schema."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that rejects duplicate mapping keys.

    Plain ``yaml.safe_load`` silently keeps the LAST occurrence of a
    duplicated key, which would let a second definition of a seat (or of
    ``seats:`` itself) shadow the first without any diagnostic.  The registry
    reports that as a schema error instead.
    """


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True)
class Seat:
    """One named model seat and its adapter configuration."""

    name: str
    provider: str
    model: str
    auth_env: str | None
    adapter: str
    options: Mapping[str, Any] = field(default_factory=dict)

def _error(path: Path, detail: str) -> SeatRegistryError:
    return SeatRegistryError(f"{path}: {detail}")


def _required_string(entry: Mapping[str, Any], key: str, seat_name: str, path: Path) -> str:
    if key not in entry:
        raise _error(path, f"seat {seat_name!r} is missing required field {key!r}")
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise _error(path, f"seat {seat_name!r} field {key!r} must be a non-empty string")
    return value.strip()


def _validate_placeholders(value: str, seat_name: str, path: Path) -> None:
    remainder = value.replace("{model}", "").replace("{prompt_file}", "")
    if "{" in remainder or "}" in remainder:
        raise _error(
            path,
            f"seat {seat_name!r} shell argv uses an unsupported placeholder; "
            "only {model} and {prompt_file} are allowed",
        )


def _parse_seat(name: str, raw: object, path: Path) -> Seat:
    if not isinstance(name, str) or not name.strip():
        raise _error(path, "seat names must be non-empty strings")
    if not isinstance(raw, dict):
        raise _error(path, f"seat {name!r} must be a mapping")

    provider = _required_string(raw, "provider", name, path)
    model = _required_string(raw, "model", name, path)
    adapter = _required_string(raw, "adapter", name, path)
    if adapter not in SUPPORTED_ADAPTERS:
        supported = ", ".join(sorted(SUPPORTED_ADAPTERS))
        raise _error(path, f"seat {name!r} has unknown adapter {adapter!r}; expected one of {supported}")

    if "auth_env" not in raw:
        raise _error(path, f"seat {name!r} is missing required field 'auth_env'")
    auth_env = raw["auth_env"]
    if auth_env is not None and (not isinstance(auth_env, str) or not auth_env.strip()):
        raise _error(path, f"seat {name!r} field 'auth_env' must be null or a non-empty string")
    if isinstance(auth_env, str):
        auth_env = auth_env.strip()

    options_raw = raw.get("options", {})
    if options_raw is None:
        options_raw = {}
    if not isinstance(options_raw, dict):
        raise _error(path, f"seat {name!r} field 'options' must be a mapping")
    options: dict[str, Any] = dict(options_raw)

    if adapter == "openai-compatible-endpoint":
        base_url_env = options.get("base_url_env")
        if not isinstance(base_url_env, str) or not base_url_env.strip():
            raise _error(
                path,
                f"seat {name!r} openai-compatible-endpoint requires "
                "options.base_url_env as a non-empty string",
            )
        protocol = options.get("protocol")
        if protocol not in SUPPORTED_OPENAI_PROTOCOLS:
            allowed = ", ".join(sorted(SUPPORTED_OPENAI_PROTOCOLS))
            raise _error(
                path,
                f"seat {name!r} options.protocol must be one of {allowed}",
            )
        options["base_url_env"] = base_url_env.strip()

    if adapter == "shell-command":
        argv = options.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(part, str) and part for part in argv
        ):
            raise _error(
                path,
                f"seat {name!r} shell-command requires options.argv as a non-empty list of strings",
            )
        for part in argv:
            _validate_placeholders(part, name, path)
        options["argv"] = list(argv)

    return Seat(
        name=name.strip(),
        provider=provider,
        model=model,
        auth_env=auth_env,
        adapter=adapter,
        options=MappingProxyType(options),
    )


def _read_registry(path: Path) -> dict[str, Seat]:
    if not path.is_file():
        raise FileNotFoundError(f"seat registry not found: {path}")
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise _error(path, f"invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise _error(path, "document must contain a mapping at the top level")
    version = document.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise _error(path, f"schema_version must be {SCHEMA_VERSION}, got {version!r}")
    seats_raw = document.get("seats")
    if not isinstance(seats_raw, dict):
        raise _error(path, "'seats' must be a mapping")
    return {name: _parse_seat(name, raw, path) for name, raw in seats_raw.items()}


class SeatRegistry(Mapping[str, Seat]):
    """Immutable mapping of seat name to :class:`Seat`.

    ``SeatRegistry(path)`` and ``SeatRegistry.load(path)`` are equivalent;
    accepting both keeps construction natural for CLI and library callers.
    """

    def __init__(self, source: Path | str | Mapping[str, Seat]) -> None:
        if isinstance(source, (str, Path)):
            seats = _read_registry(Path(source))
        else:
            seats = dict(source)
            if not all(isinstance(name, str) and isinstance(seat, Seat) for name, seat in seats.items()):
                raise TypeError("SeatRegistry mapping values must be Seat instances")
        self._seats: Mapping[str, Seat] = MappingProxyType(seats)

    @classmethod
    def load(cls, path: Path | str) -> "SeatRegistry":
        return cls(path)

    @classmethod
    def from_config(cls, config: object) -> "SeatRegistry":
        root = getattr(config, "root")
        seats_file = getattr(config, "seats_file")
        return cls(Path(root) / seats_file)

    @property
    def seats(self) -> Mapping[str, Seat]:
        return self._seats

    def require(self, name: str) -> Seat:
        try:
            return self._seats[name]
        except KeyError:
            raise SeatRegistryError(f"seat {name!r} is not defined in the registry") from None

    def __getitem__(self, name: str) -> Seat:
        return self._seats[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._seats)

    def __len__(self) -> int:
        return len(self._seats)


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_ADAPTERS",
    "Seat",
    "SeatRegistry",
    "SeatRegistryError",
]
