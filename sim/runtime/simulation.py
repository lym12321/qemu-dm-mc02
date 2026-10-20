"""Bounded synchronous cut scheduling, independent of any machine or device.

This is sampled co-simulation: interval outputs become eligible at its end.
Exact machine cuts, transport delivery and firmware consumption remain distinct.
Synchronous third-party methods must return; Python cannot preempt those calls.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import heapq
import math
from time import monotonic
from types import MappingProxyType
from typing import Any

from sim.api import Endpoint, Event, NodeDescription, Session, StepContext, StepResult, TimeQuality, Topology
from sim.api.events import check_timestamp
from sim.api.topology import immutable
from sim.fabric import CanBus, CanDelivery, CanFrame


class SimulationState(StrEnum):
    NEW = "new"
    PAUSED = "paused"
    RUNNING = "running"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True)
class _Input:
    endpoint: Endpoint
    payload: Any


class CanConnection:
    """Borrow one endpoint and queue; every send obeys the active cut floor."""

    def __init__(self, context: ComponentContext, port: str, bus: CanBus, endpoint: Any):
        self._context = context
        self.port = port
        self.bus = bus
        self._endpoint = endpoint

    def send(self, frame: CanFrame) -> None:
        self._context._check_active()
        sim = self._context._simulation
        if sim._phase == "creating" or sim._closing:
            raise RuntimeError("CAN sends require completed component wiring")
        self.bus.send(self._endpoint, frame, not_before_ns=sim._not_before_ns)
        self._context.emit(self.port, "can.sent", {"frame": frame},
                           source_time_ns=frame.timestamp_ns,
                           mapped_time_ns=frame.timestamp_ns)


class ComponentContext:
    """Factory access to admitted wiring, observations and a shared deadline.

    Connections and components are borrowed. Only Simulation closes instances.
    A factory owns partial construction until it returns an instance; it must
    release those partial resources itself if construction raises.
    """

    def __init__(self, simulation: Simulation, node_id: str):
        self._simulation = simulation
        self.node_id = node_id
        self.session = simulation.session
        self._active = True
        self._connections: dict[str, CanConnection] = {}

    def _check_active(self) -> None:
        if not self._active or self.session is not self._simulation.session:
            raise RuntimeError("component context belongs to a closed session")

    @property
    def deadline_monotonic(self) -> float:
        self._check_active()
        return self._simulation._deadline

    @property
    def t_sim_ns(self) -> int:
        self._check_active()
        return self._simulation._dispatch_time_ns

    @property
    def not_before_ns(self) -> int:
        self._check_active()
        return self._simulation._not_before_ns

    @property
    def connected_ports(self) -> tuple[str, ...]:
        return tuple(port.name for port in self._simulation._descriptions[self.node_id].ports
                     if self._simulation._peers.get(Endpoint(self.node_id, port.name)))

    def check_deadline(self) -> None:
        self._check_active()
        self._simulation._check_deadline()

    def peers(self, port: str) -> tuple[Endpoint, ...]:
        self._check_active()
        self._simulation._descriptions[self.node_id].port(port)
        return self._simulation._peers.get(Endpoint(self.node_id, port), ())

    def component(self, node_id: str) -> Any:
        """Borrow a fully constructed peer, normally from the start hook."""
        self._check_active()
        if self._simulation._phase == "creating":
            raise RuntimeError("component lookup requires completed construction")
        return self._simulation._components[node_id]

    def can_bus(self, port: str) -> CanBus:
        peers = self.peers(port)
        description = self._simulation._descriptions[self.node_id].port(port)
        if description.category != "protocol" or description.protocol != "can":
            raise ValueError(f"{self.node_id}.{port} is not a CAN protocol port")
        if len(peers) != 1:
            raise ValueError(f"{self.node_id}.{port} requires exactly one connected CAN bus")
        peer = peers[0]
        if "can_bus" not in self._simulation._descriptions[peer.node].capabilities:
            raise ValueError(f"{self.node_id}.{port} is not connected to a CAN bus")
        bus = self._simulation._components.get(peer.node)
        if not isinstance(bus, CanBus):
            raise TypeError(f"CAN bus factory {peer.node} did not return a CanBus")
        return bus

    def attach_can(self, port: str, endpoint: Any) -> CanConnection:
        self._check_active()
        if port in self._connections:
            raise ValueError(f"CAN port is already attached: {self.node_id}.{port}")
        bus = self.can_bus(port)
        bus.attach(endpoint, f"{self.node_id}.{port}")
        connection = CanConnection(self, port, bus, endpoint)
        self._connections[port] = connection
        return connection

    def emit(self, port: str | None, kind: str, payload: Any, *,
             source_time_ns: int | None = None, mapped_time_ns: int | None = None,
             delivery_time_ns: int | None = None,
             quality: TimeQuality | str = TimeQuality.EXACT) -> Event:
        self._check_active()
        if port is not None:
            self._simulation._descriptions[self.node_id].port(port)
        return self._simulation._emit(self.node_id, port, kind, payload,
                                      source_time_ns=source_time_ns,
                                      mapped_time_ns=mapped_time_ns,
                                      delivery_time_ns=delivery_time_ns,
                                      quality=quality)


class Simulation:
    """Own all topology instances and one integer-nanosecond logical clock.

    ``describe`` is pure. ``create`` constructs buses first, then other nodes in
    manifest order. After all wiring, optional reset(Session) and start() hooks
    run. Machine capability requires request_step/finish_step; step capability
    requires step. Both return exact-target StepResult values. Every instance
    requires close(). No implicit retries or cross-component rollback occur.
    """

    def __init__(self, topology: Topology, registry: Mapping[str, Any], *,
                 quantum_ns: int = 1_000_000, mode: str = "stepped",
                 startup_timeout_s: float = 10, host_timeout_s: float | None = None,
                 max_pending_inputs: int = 256, dispatch_budget: int = 4096,
                 event_history: int = 256,
                 on_event: Callable[[Event], None] | None = None):
        if not isinstance(topology, Topology):
            raise TypeError("Simulation requires a Topology")
        check_timestamp(quantum_ns)
        if quantum_ns == 0:
            raise ValueError("quantum_ns must be positive")
        if mode not in ("stepped", "interactive"):
            raise ValueError("mode must be stepped or interactive")
        if host_timeout_s is None:
            host_timeout_s = 300 if mode == "interactive" else 30
        for name, value in (("startup_timeout_s", startup_timeout_s),
                            ("host_timeout_s", host_timeout_s)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a finite positive number")
        for name, value in (("max_pending_inputs", max_pending_inputs),
                            ("dispatch_budget", dispatch_budget), ("event_history", event_history)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be callable or None")
        self.topology = topology
        self.registry = MappingProxyType(dict(registry))
        self.quantum_ns = quantum_ns
        self.mode = mode
        self.startup_timeout_s = startup_timeout_s
        self.host_timeout_s = host_timeout_s
        self.max_pending_inputs = max_pending_inputs
        self.dispatch_budget = dispatch_budget
        self.on_event = on_event
        self._descriptions = topology.validate(self.registry)
        self._validate_execution()
        self._peers: dict[Endpoint, tuple[Endpoint, ...]] = {}
        for link in topology.links:
            for endpoint, peer in ((link.source, link.target), (link.target, link.source)):
                self._peers[endpoint] = (*self._peers.get(endpoint, ()), peer)
        self.state = SimulationState.NEW
        self.session: Session | None = None
        self.t_sim_ns = 0
        self.step_id = 0
        self.failure: BaseException | None = None
        self._components: dict[str, Any] = {}
        self._contexts: dict[str, ComponentContext] = {}
        self._owned: list[tuple[str, Any]] = []
        self._inputs: list[tuple[int, int, _Input]] = []
        self._input_sequence = 0
        self._events: deque[Event] = deque(maxlen=event_history)
        self.events_total = 0
        self.events_dropped = 0
        self._deadline = math.inf
        self._host_deadline = math.inf
        self._dispatch_time_ns = 0
        self._not_before_ns = 0
        self._phase = "new"
        self._stepping = False
        self._closing = False
        self._pause_requested = False

    def _validate_execution(self) -> None:
        for name, description in self._descriptions.items():
            caps = description.capabilities
            if "machine" in caps and "step" in caps:
                raise ValueError(f"{name} cannot own both machine and component step capabilities")
            if "can_bus" in caps and ("machine" in caps or "step" in caps):
                raise ValueError(f"{name} cannot independently advance a CAN bus")
        for link in self.topology.links:
            first = self._descriptions[link.source.node]
            second = self._descriptions[link.target.node]
            port = first.port(link.source.port)
            if port.category == "protocol" and port.protocol == "can":
                if ("can_bus" in first.capabilities) == ("can_bus" in second.capabilities):
                    raise ValueError("each CAN link requires one bus and one endpoint")
                if port.direction != "inout" or second.port(link.target.port).direction != "inout":
                    raise ValueError("runtime CAN connections require inout ports")
            else:
                raise ValueError(f"runtime does not support linked {port.category}/{port.protocol} ports")

    def validate(self) -> Mapping[str, NodeDescription]:
        """Return static admission results without constructing any instance."""
        return self._descriptions

    @property
    def components(self) -> Mapping[str, Any]:
        return MappingProxyType(self._components)

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def _check_deadline(self) -> None:
        if monotonic() >= self._deadline:
            raise TimeoutError(f"simulation host deadline exceeded during {self._phase}")

    def check_deadline(self) -> None:
        """Check the same host budget used by stepped and interactive execution."""
        if self.state not in (SimulationState.PAUSED, SimulationState.RUNNING):
            raise RuntimeError("deadline checks require a started, healthy Simulation")
        try:
            self._check_deadline()
        except BaseException as exc:
            self.failure = exc
            self.state = SimulationState.FAILED
            raise

    def _emit(self, node: str, port: str | None, kind: str, payload: Any, *,
              source_time_ns: int | None = None, mapped_time_ns: int | None = None,
              delivery_time_ns: int | None = None,
              quality: TimeQuality | str = TimeQuality.EXACT) -> Event:
        if self.session is None:
            raise RuntimeError("observations require an active session")
        mapped = self._dispatch_time_ns if mapped_time_ns is None else mapped_time_ns
        delivery = self._dispatch_time_ns if delivery_time_ns is None else delivery_time_ns
        event = Event(self.session.session_id, self.events_total, node, port, kind,
                      payload, source_time_ns, mapped, delivery, quality)
        if len(self._events) == self._events.maxlen:
            self.events_dropped += 1
        self._events.append(event)
        self.events_total += 1
        if self.on_event is not None:
            self.on_event(event)
        return event

    def _bus_log(self, delivery: CanDelivery) -> None:
        node, _, port = delivery.source.partition(".")
        self._emit(node, port or None, "can.delivery", {
            "destination": delivery.destination, "frame": delivery.frame,
            "bus_sequence": delivery.sequence, "due_ns": delivery.due_ns,
            "late": delivery.late, "dropped": delivery.dropped,
        }, source_time_ns=delivery.timestamp_ns,
            mapped_time_ns=delivery.timestamp_ns,
            delivery_time_ns=delivery.delivered_ns)

    def _create_resources(self) -> None:
        self._phase = "creating"
        nodes = sorted(self.topology.nodes,
                       key=lambda node: "can_bus" not in self._descriptions[node.name].capabilities)
        for node in nodes:
            self._check_deadline()
            context = ComponentContext(self, node.name)
            self._contexts[node.name] = context
            component = self.registry[node.kind].create(node, context)
            if any(component is other for _, other in self._owned):
                raise ValueError("each component instance must have exactly one owner")
            # Register immediately, including objects whose method admission fails.
            self._owned.append((node.name, component))
            self._components[node.name] = component
            self._check_deadline()
            methods = ["close"]
            caps = self._descriptions[node.name].capabilities
            if "machine" in caps:
                methods += ["request_step", "finish_step"]
            if "step" in caps:
                methods += ["step"]
            if "input" in caps:
                methods += ["input"]
            if any(not callable(getattr(component, method, None)) for method in methods):
                raise TypeError(f"component {node.name} lacks capability methods: {', '.join(methods)}")
            if "can_bus" in caps:
                if not isinstance(component, CanBus):
                    raise TypeError(f"CAN bus factory {node.name} must return a CanBus")
                previous_log = component.log

                def log(delivery, previous=previous_log):
                    if previous is not None:
                        previous(delivery)
                    self._bus_log(delivery)

                component.log = log
        for endpoint in self._peers:
            description = self._descriptions[endpoint.node]
            if "can_bus" not in description.capabilities:
                if endpoint.port not in self._contexts[endpoint.node]._connections:
                    raise ValueError(f"factory did not attach its connected CAN endpoint: {endpoint}")
        self._phase = "initializing"
        for name, component in self._owned:
            self._check_deadline()
            reset = getattr(component, "reset", None)
            if "can_bus" in self._descriptions[name].capabilities:
                component.reset(self.session.t_sim_ns)
            elif callable(reset):
                reset(self.session)
            self._check_deadline()
        for _, component in self._owned:
            self._check_deadline()
            start = getattr(component, "start", None)
            if callable(start):
                start()
            self._check_deadline()

    def start(self) -> None:
        if self.state != SimulationState.NEW or self._phase != "new":
            raise RuntimeError("start requires a new Simulation")
        # Startup and active host budgets include the first resource creation.
        begun = monotonic()
        self._host_deadline = begun + self.host_timeout_s
        self._deadline = min(self._host_deadline, begun + self.startup_timeout_s)
        self.session = Session(1)
        try:
            self._create_resources()
            self._check_deadline()
        except BaseException as exc:
            self.failure = exc
            self.state = SimulationState.FAILED
            self._close_owned(exc)
            self._phase = "failed"
            raise
        self._deadline = self._host_deadline
        self._phase = "paused"
        self.state = SimulationState.PAUSED

    def schedule_input(self, endpoint: Endpoint | str, payload: Any, *, at_ns: int | None = None) -> None:
        if self.state not in (SimulationState.PAUSED, SimulationState.RUNNING):
            raise RuntimeError("inputs require a started, healthy Simulation")
        endpoint = Endpoint.parse(endpoint)
        if endpoint.node not in self._descriptions:
            raise ValueError(f"unknown input node {endpoint.node}")
        description = self._descriptions[endpoint.node]
        port = description.port(endpoint.port)
        if "input" not in description.capabilities or port.direction == "out":
            raise ValueError(f"endpoint cannot accept external inputs: {endpoint}")
        when = self._dispatch_time_ns if at_ns is None else at_ns
        check_timestamp(when)
        if when < self._dispatch_time_ns:
            raise ValueError("input time must not precede the current dispatch cut")
        value = immutable(payload)
        if len(self._inputs) >= self.max_pending_inputs:
            raise BufferError("simulation input capacity exhausted")
        heapq.heappush(self._inputs, (when, self._input_sequence, _Input(endpoint, value)))
        self._input_sequence += 1

    def _settle(self, cut: int) -> None:
        self._dispatch_time_ns = cut
        self._not_before_ns = cut
        remaining = self.dispatch_budget
        buses = tuple(component for name, component in self._owned
                      if "can_bus" in self._descriptions[name].capabilities)
        bus_budgets = [bus.dispatch_budget for bus in buses]
        while True:
            self._check_deadline()
            while self._inputs and self._inputs[0][0] <= cut:
                if remaining <= 0:
                    raise RuntimeError("simulation input dispatch budget exhausted (possible event loop)")
                when, _, item = heapq.heappop(self._inputs)
                self._components[item.endpoint.node].input(item.endpoint.port, item.payload)
                self._emit(item.endpoint.node, item.endpoint.port, "input.delivered", item.payload,
                           source_time_ns=when, mapped_time_ns=when, delivery_time_ns=cut)
                self._check_deadline()
                remaining -= 1
            for index, bus in enumerate(buses):
                self._check_deadline()
                if remaining <= 0 and bus.has_due(cut):
                    raise RuntimeError("simulation CAN dispatch budget exhausted (possible event loop)")
                dispatched = bus.step(cut, max_dispatch=min(remaining, bus_budgets[index]))
                remaining -= dispatched
                bus_budgets[index] -= dispatched
                if bus.has_due(cut):
                    raise RuntimeError("CAN dispatch budget exhausted (possible event loop)")
                self._check_deadline()
            if (not (self._inputs and self._inputs[0][0] <= cut)
                    and not any(bus.has_due(cut) for bus in buses)):
                return

    @staticmethod
    def _reached(name: str, result: Any, ctx: StepContext) -> None:
        if (not isinstance(result, StepResult) or result.status != "ok"
                or type(result.reached_time_ns) is not int
                or result.reached_time_ns != ctx.t_sim_ns):
            raise RuntimeError(f"component {name} did not reach exact target: {result!r}")

    def _step(self, target: int) -> dict[str, StepResult]:
        """Execute one admitted interval; its start cut is already settled."""
        ctx = StepContext(self.session.session_id, self.step_id + 1,
                          target, target - self.t_sim_ns)
        results = {}
        self._dispatch_time_ns = target
        self._not_before_ns = target
        self._phase = "machine request"
        for name, component in self._owned:
            if "machine" in self._descriptions[name].capabilities:
                self._check_deadline()
                component.request_step(ctx)
                self._check_deadline()
        self._phase = "machine completion"
        for name, component in self._owned:
            if "machine" in self._descriptions[name].capabilities:
                self._check_deadline()
                result = component.finish_step(ctx)
                self._reached(name, result, ctx)
                results[name] = result
                self._check_deadline()
        self._phase = "component step"
        for name, component in self._owned:
            if "step" in self._descriptions[name].capabilities:
                self._check_deadline()
                result = component.step(ctx)
                self._reached(name, result, ctx)
                results[name] = result
                self._check_deadline()
        self._phase = "target delivery"
        self._settle(target)
        self._check_deadline()
        self.t_sim_ns = target
        self.step_id = ctx.step_id
        return results

    def _advance_to(self, target: int) -> StepResult:
        """Share bounded event cuts and pause semantics across public drivers."""
        if self._stepping:
            raise RuntimeError("Simulation execution cannot be reentered")
        self._pause_requested = False
        self.state = SimulationState.RUNNING
        self._stepping = True
        results = {}
        completed = 0
        try:
            self._phase = "start delivery"
            self._settle(self.t_sim_ns)
            while self.t_sim_ns < target and not self._pause_requested:
                cut = min(target, self.t_sim_ns + self.quantum_ns)
                if self._inputs:
                    cut = min(cut, self._inputs[0][0])
                for name, component in self._owned:
                    if "can_bus" in self._descriptions[name].capabilities:
                        due = component.next_due_ns
                        if due is not None:
                            cut = min(cut, due)
                results = self._step(cut)
                completed += 1
            self._check_deadline()
            status = "paused" if self.t_sim_ns < target else "ok"
            return StepResult(status, self.t_sim_ns, {
                "requested_time_ns": target, "step_id": self.step_id,
                "substeps_completed": completed, "components": results,
            })
        except BaseException as exc:
            exc.add_note(f"simulation failed during {self._phase}, step {self.step_id + 1}; completed time remains {self.t_sim_ns}")
            self.failure = exc
            self.state = SimulationState.FAILED
            raise
        finally:
            self._stepping = False
            if self.state == SimulationState.RUNNING:
                self.state = SimulationState.PAUSED
                self._phase = "paused"

    def step(self, dt_ns: int | None = None) -> StepResult:
        if self.state != SimulationState.PAUSED:
            raise RuntimeError("step requires a paused, healthy Simulation")
        dt_ns = self.quantum_ns if dt_ns is None else dt_ns
        check_timestamp(dt_ns)
        if dt_ns == 0:
            raise ValueError("dt_ns must be positive")
        target = self.t_sim_ns + dt_ns
        check_timestamp(target)
        return self._advance_to(target)

    def run_until(self, target_ns: int) -> StepResult:
        if self.state != SimulationState.PAUSED:
            raise RuntimeError("run_until requires a paused, healthy Simulation")
        check_timestamp(target_ns)
        if target_ns < self.t_sim_ns:
            raise ValueError("run target must not precede completed simulation time")
        return self._advance_to(target_ns)

    def pause(self) -> None:
        if self.state not in (SimulationState.PAUSED, SimulationState.RUNNING):
            raise RuntimeError("pause requires a started, healthy Simulation")
        self._pause_requested = True

    def _close_owned(self, primary: BaseException | None = None) -> BaseException | None:
        self._closing = True
        for name, component in reversed(self._owned):
            try:
                close = getattr(component, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"component {name} cleanup failed: {type(exc).__name__}: {exc}")
                    if primary is not exc:
                        for note in getattr(exc, "__notes__", ()):
                            primary.add_note(note)
        for context in self._contexts.values():
            context._active = False
        self._owned.clear()
        self._components.clear()
        self._contexts.clear()
        self._closing = False
        return primary

    def reset(self) -> None:
        """Destroy and recreate every owned instance; failed runs require a new object."""
        if self.state != SimulationState.PAUSED or self._stepping:
            raise RuntimeError("reset requires a paused, healthy Simulation")
        previous_session = self.session.session_id
        self.state = SimulationState.RUNNING
        primary = self._close_owned()
        if primary is not None:
            self.failure = primary
            self.state = SimulationState.FAILED
            raise primary
        self._inputs.clear()
        self.t_sim_ns = self.step_id = self._dispatch_time_ns = self._not_before_ns = 0
        self.session = Session(previous_session + 1)
        self._deadline = min(self._host_deadline, monotonic() + self.startup_timeout_s)
        try:
            self._create_resources()
            self._check_deadline()
        except BaseException as exc:
            self.failure = exc
            self.state = SimulationState.FAILED
            self._close_owned(exc)
            self._phase = "failed"
            raise
        self._deadline = self._host_deadline
        self._phase = "paused"
        self.state = SimulationState.PAUSED

    def close(self) -> None:
        if self.state == SimulationState.CLOSED:
            return
        if self._stepping or self._phase in ("creating", "initializing") or self._closing:
            raise RuntimeError("close cannot interrupt active construction, execution or cleanup")
        self._phase = "cleanup"
        self.state = SimulationState.CLOSED
        primary = self._close_owned()
        self._inputs.clear()
        if primary is not None:
            if self.failure is not None:
                self.failure.add_note(f"simulation cleanup failed: {type(primary).__name__}: {primary}")
                for note in getattr(primary, "__notes__", ()):
                    self.failure.add_note(note)
                raise self.failure
            raise primary

    def __enter__(self) -> Simulation:
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.close()
        except BaseException as cleanup:
            if exc is None:
                raise
            if cleanup is not exc:
                exc.add_note(f"simulation cleanup failed: {type(cleanup).__name__}: {cleanup}")
                for note in getattr(cleanup, "__notes__", ()):
                    exc.add_note(note)
        return False
