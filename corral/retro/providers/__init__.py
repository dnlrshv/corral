"""Provider adapters and adapter selection for retrospective seats."""

from corral.retro.providers.anthropic import AnthropicSeatRunner
from corral.retro.providers.base import Availability, SeatResult, SeatRunner, SeatStatus
from corral.retro.providers.openai_compatible import OpenAICompatibleSeatRunner
from corral.retro.providers.shell import ShellSeatRunner
from corral.retro.seats import Seat


def runner_for_seat(seat: Seat) -> SeatRunner:
    """Construct the adapter runner declared by *seat*."""
    runners = {
        "anthropic-sdk": AnthropicSeatRunner,
        "openai-compatible-endpoint": OpenAICompatibleSeatRunner,
        "shell-command": ShellSeatRunner,
    }
    try:
        return runners[seat.adapter]()
    except KeyError:
        raise ValueError(f"unsupported seat adapter: {seat.adapter!r}") from None


__all__ = [
    "AnthropicSeatRunner",
    "Availability",
    "OpenAICompatibleSeatRunner",
    "SeatResult",
    "SeatRunner",
    "SeatStatus",
    "ShellSeatRunner",
    "runner_for_seat",
]
