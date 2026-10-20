"""Completed observations with explicit timestamp provenance."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .topology import immutable


class TimeQuality(StrEnum):
    EXACT = "exact"
    SAMPLED = "sampled"
    LOWER_BOUND = "lower_bound"
    UPPER_BOUND = "upper_bound"


def check_timestamp(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("simulation time must be an unsigned 64-bit integer nanosecond value")


@dataclass(frozen=True)
class Event:
    session_id: int
    sequence: int
    source_node: str
    source_port: str | None
    kind: str
    payload: Any
    source_time_ns: int | None
    mapped_time_ns: int
    delivery_time_ns: int
    quality: TimeQuality | str

    def __post_init__(self) -> None:
        if type(self.session_id) is not int or self.session_id <= 0:
            raise ValueError("event session must be a positive integer")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("event sequence must be a nonnegative integer")
        if self.source_time_ns is not None:
            check_timestamp(self.source_time_ns)
        check_timestamp(self.mapped_time_ns)
        check_timestamp(self.delivery_time_ns)
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("event kind must be a nonempty string")
        object.__setattr__(self, "quality", TimeQuality(self.quality))
        object.__setattr__(self, "payload", immutable(self.payload))
