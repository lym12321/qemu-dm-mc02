"""Explicit CAN factory; description and option admission are resource free."""

from sim.api import NodeDescription, PortSpec

from .can import CanBus, check_can_time


class CanBusFactory:
    @staticmethod
    def describe(spec):
        allowed = {"delay_ns", "loopback", "max_pending", "dispatch_budget"}
        unknown = set(spec.options) - allowed
        if unknown:
            raise ValueError(f"unknown CAN bus options: {', '.join(sorted(unknown))}")
        check_can_time(spec.options.get("delay_ns", 0))
        if type(spec.options.get("loopback", False)) is not bool:
            raise ValueError("CAN loopback must be a boolean")
        for key in ("max_pending", "dispatch_budget"):
            value = spec.options.get(key, 256)
            if type(value) is not int or not 0 < value <= 65536:
                raise ValueError(f"CAN {key} must be an integer in 1..65536")
        return NodeDescription((PortSpec("can", "can", max_connections=None),),
                               frozenset({"can_bus"}))

    @staticmethod
    def create(spec, context):
        return CanBus(**spec.options)
