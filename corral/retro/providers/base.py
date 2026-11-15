"""Common result types and interface for seat provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from corral.retro.seats import Seat


class SeatStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    TIMEOUT = "timeout"
    INCOMPLETE = "incomplete"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Availability:
    status: SeatStatus
    provider: str = ""
    model: str = ""
    detail: str = ""
    seat: str = ""

    @property
    def available(self) -> bool:
        return SeatStatus(self.status) is SeatStatus.OK

    @property
    def reason(self) -> str:
        """Compatibility/readability alias for the diagnostic detail."""
        return self.detail


@dataclass(frozen=True)
class SeatResult:
    text: str
    provider: str
    model: str
    status: SeatStatus
    detail: str = ""
    seat: str = ""

    @property
    def ok(self) -> bool:
        return SeatStatus(self.status) is SeatStatus.OK


def availability(seat: Seat, status: SeatStatus, detail: str = "") -> Availability:
    return Availability(status, seat.provider, seat.model, detail, seat.name)


def result(seat: Seat, status: SeatStatus, text: str = "", detail: str = "") -> SeatResult:
    return SeatResult(text, seat.provider, seat.model, status, detail, seat.name)


def _seat_max_tokens(seat: Seat, max_tokens: int) -> int:
    return int(seat.options["max_tokens"]) if "max_tokens" in seat.options else max_tokens


class SeatRunner(ABC):
    """A minimal provider-independent completion interface."""

    @abstractmethod
    def probe(self, seat: Seat) -> Availability:
        """Report whether *seat* is ready to invoke without generating text."""

    @abstractmethod
    def complete(
        self,
        seat: Seat,
        prompt: str,
        *,
        timeout: float,
        max_tokens: int,
    ) -> SeatResult:
        """Complete *prompt*, folding all expected adapter failures into status."""


__all__ = [
    "Availability",
    "SeatResult",
    "SeatRunner",
    "SeatStatus",
    "availability",
    "result",
]
