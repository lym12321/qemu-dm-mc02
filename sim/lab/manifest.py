"""Immutable native interactive Lab inputs, without scheduling or IO ownership."""

from __future__ import annotations

from dataclasses import dataclass

from sim.fabric import CanFrame


@dataclass(frozen=True)
class Stimulus:
    """One scheduled uart/key/gpio/adc_raw/adc_voltage/can input."""

    at_ns: int
    endpoint: str
    kind: str
    value: bytes | bool | int | CanFrame
