"""Finite experiments over sim/v1, driven exclusively by Simulation.

Inputs use the runtime's queue and cuts. A resource-free sample consumer marks
already completed cuts for assertion expiry; it never advances CPU or physics.
Matching reuses the pure Lab evaluator, without importing a legacy runner.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sim.api import Endpoint, Event, NodeDescription, NodeSpec, TimeQuality, Topology
from sim.api.events import check_timestamp
from sim.api.topology import immutable
from sim.boards import digital_level
from sim.config.scene import Scene, load_scene
from sim.fabric import CanFrame
from sim.gpio import validate_gpio_pin
from sim.lab.experiment import AssertionEvaluator, AssertionObservation
from sim.lab.manifest import MAX_ITEMS, parse_assertion
from sim.runtime import Simulation


@dataclass(frozen=True)
class ExperimentInput:
    at_ns: int
    endpoint: Endpoint | str
    payload: object

    def __post_init__(self):
        check_timestamp(self.at_ns)
        object.__setattr__(self, "endpoint", Endpoint.parse(self.endpoint))
        object.__setattr__(self, "payload", immutable(self.payload))


@dataclass(frozen=True)
class ObservationReceipt:
    """The matching public Event's timestamps, unchanged and explicitly named."""

    assertion_index: int
    session_id: int
    event_sequence: int
    source_node: str
    source_port: str | None
    kind: str
    source_time_ns: int | None
    mapped_time_ns: int
    delivery_time_ns: int
    quality: TimeQuality


@dataclass(frozen=True)
class ExperimentResult:
    status: tuple[str, ...]
    failures: tuple[str, ...]
    matches: tuple[ObservationReceipt, ...]
    end_ns: int
    delivered_inputs: int
    events_total: int

    @property
    def passed(self):
        return not self.failures and all(item == "pass" for item in self.status)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    return value


class _Consumer:
    def __init__(self, assertions, duration_ns, snapshot_endpoints, observations):
        self.evaluator = AssertionEvaluator(assertions, duration_ns, 0)
        self.snapshot_endpoints = snapshot_endpoints
        self.observations = observations
        self.cut = None
        self.sequence = -1
        self.matches = []
        self.delivered_inputs = 0

    def begin_cut(self, cut):
        check_timestamp(cut)
        if self.cut is not None and cut < self.cut:
            raise ValueError("experiment observation cuts moved backwards")
        if self.cut is not None and cut > self.cut:
            self.evaluator.advance(self.cut)
        self.cut = cut

    def __call__(self, event):
        if not isinstance(event, Event) or event.session_id != 1 or event.sequence != self.sequence + 1:
            raise ValueError("experiment requires ordered Events from its one Simulation session")
        self.begin_cut(event.delivery_time_ns)
        self.sequence = event.sequence
        if event.kind == "input.delivered":
            self.delivered_inputs += 1
        endpoint = (f"{event.source_node}.{event.source_port}"
                    if event.source_port is not None else None)
        digital, observed = {}, ()
        if event.kind == "snapshot" and event.source_node in self.snapshot_endpoints:
            if event.mapped_time_ns != event.delivery_time_ns:
                raise ValueError("digital snapshot requires its completed observation cut")
            snapshot = _plain(event.payload)
            digital = {qualified: digital_level(local, snapshot)
                       for qualified, local in self.snapshot_endpoints[event.source_node]}
        elif event.kind in ("uart", "net.uart.delivery"):
            data = event.payload
            if event.kind == "net.uart.delivery":
                endpoint, data = event.payload["source"], event.payload["data"]
            if self.observations.get(endpoint) != event.kind:
                return
            if (endpoint is None or type(data) is not bytes
                    or event.quality == TimeQuality.LOWER_BOUND):
                raise ValueError("UART assertion requires bytes with an observation upper bound")
            observed = (AssertionObservation(endpoint, event.mapped_time_ns, data),)
        elif event.kind in ("can-tx", "can.delivery"):
            if self.observations.get(endpoint) != event.kind:
                return
            if event.kind == "can.delivery" and event.payload["dropped"]:
                return
            if event.quality == TimeQuality.LOWER_BOUND:
                raise ValueError("CAN assertion requires an observation upper bound")
            frame = event.payload if event.kind == "can-tx" else event.payload.get("frame")
            if not isinstance(frame, CanFrame):
                raise TypeError("CAN observation requires the actual CanFrame")
            observed = (AssertionObservation(endpoint, event.mapped_time_ns, frame),)
        elif event.kind in ("digital", "net.digital.sample") and endpoint is not None:
            if self.observations.get(endpoint) != "digital":
                return
            if event.mapped_time_ns != event.delivery_time_ns:
                raise ValueError("digital sample requires its completed observation cut")
            level = event.payload if event.kind == "digital" else event.payload["level"]
            if level is not None:
                digital[endpoint] = level
        if not digital and not observed:
            return
        before = self.evaluator.status
        self.evaluator.observe_values(digital, observed, event.delivery_time_ns, expire=False)
        for index, (old, new) in enumerate(zip(before, self.evaluator.status)):
            if old == "pending" and new == "pass":
                self.matches.append(ObservationReceipt(index, event.session_id, event.sequence,
                    event.source_node, event.source_port, event.kind, event.source_time_ns,
                    event.mapped_time_ns, event.delivery_time_ns, event.quality))


class _CutObserver:
    def __init__(self, consumer):
        self.consumer = consumer

    def sample(self, cut):
        self.consumer.begin_cut(cut)

    def close(self):
        pass


class _CutObserverFactory:
    def __init__(self, consumer):
        self.consumer = consumer

    def describe(self, _spec):
        return NodeDescription(capabilities={"sample"})

    def create(self, _spec, _context):
        return _CutObserver(self.consumer)


def run_experiment(scene: Scene | str | Path, registry, *, inputs=(), assertions=(),
                   startup_timeout_s=10, host_timeout_s=30, on_event=None) -> ExperimentResult:
    """Run one admitted sim/v1 scene and return only after Simulation cleanup.

    Assertions use the existing before/uart_contains/digital_equals/can_seen
    mapping shape with qualified node.port endpoints. Inputs are typed records;
    equal-time order is their tuple order. Assertions begin at Simulation origin0,
    independent of application ready or native source epochs. No ADC/VIN/ready
    compatibility is implied. The callback borrows each full original Event.

    Machine UART/CAN assertions consume public uart/can-tx observations, retaining
    native source epochs. Other connected producers consume successful network
    delivery events; unconnected producers use uart/can-tx. Queue acceptance
    events never count as observations. A delivery is no firmware-read proof.
    The scene may contain at most 63 nodes, reserving one for cut observation.
    """
    registry = dict(registry)
    scene = load_scene(scene, registry) if isinstance(scene, (str, Path)) else scene
    if not isinstance(scene, Scene):
        raise TypeError("experiment requires a sim/v1 Scene")
    if len(scene.topology.nodes) > 63:
        raise ValueError("experiment scene requires at most 63 nodes")
    if scene.mode != "stepped":
        raise ValueError("finite experiment requires stepped scene mode")
    check_timestamp(scene.duration_ns)
    if scene.duration_ns == 0:
        raise ValueError("experiment scene duration must be positive")
    if (not isinstance(inputs, (tuple, list)) or len(inputs) > MAX_ITEMS
            or any(type(item) is not ExperimentInput for item in inputs)
            or not isinstance(assertions, (tuple, list)) or len(assertions) > MAX_ITEMS):
        raise ValueError("experiment requires bounded typed inputs and assertion mappings")
    if on_event is not None and not callable(on_event):
        raise TypeError("experiment on_event must be callable")
    original = Simulation(scene.topology, registry, quantum_ns=scene.quantum_ns)
    descriptions = original.validate()
    specs = {node.name: node for node in scene.topology.nodes}
    snapshots = {}
    observations = {}
    connected = {str(endpoint) for link in scene.topology.links
                 for endpoint in (link.source, link.target)}

    def assertion_endpoint(kind, value):
        endpoint = Endpoint.parse(value)
        description = descriptions.get(endpoint.node)
        if description is None:
            raise ValueError(f"unknown assertion node: {endpoint.node}")
        if kind == "digital_equals":
            local = endpoint.port.removesuffix(".out")
            native_projection = local in ("key", "led") or local.startswith("gpio.")
            if native_projection and any(port.protocol == "snapshot" and port.direction == "out"
                                         for port in description.ports):
                if local.startswith("gpio."):
                    validate_gpio_pin(local[5:])
                snapshots.setdefault(endpoint.node, []).append((str(endpoint), local))
                return str(endpoint)
        port = description.port(endpoint.port)
        protocol = {"uart_contains": "uart", "can_seen": "can", "digital_equals": "digital"}[kind]
        if port.protocol != protocol or port.direction == "in":
            raise ValueError(f"assertion endpoint has no public {protocol} observations: {endpoint}")
        if protocol in ("uart", "can") and "connection" in description.capabilities:
            raise ValueError("protocol assertions must name an individual producer endpoint")
        kinds = {"uart": ("uart", "net.uart.delivery"),
                 "can": ("can-tx", "can.delivery"), "digital": ("digital", "digital")}
        direct, delivery = kinds[protocol]
        observations[str(endpoint)] = (delivery if str(endpoint) in connected
            and "machine" not in description.capabilities else direct)
        return str(endpoint)

    admitted = tuple(parse_assertion(value, index, endpoint_validator=assertion_endpoint)
                     for index, value in enumerate(assertions))
    if any(item.before_ns > scene.duration_ns for item in admitted):
        raise ValueError("assertion deadline exceeds scene duration")
    queued = []
    for item in inputs:
        if item.at_ns > scene.duration_ns or item.endpoint.node not in descriptions:
            raise ValueError("experiment input exceeds duration or names an unknown node")
        description = descriptions[item.endpoint.node]
        port = description.port(item.endpoint.port)
        if ("input" not in description.capabilities or port.direction == "out"
                or "external_input" not in port.capabilities):
            raise ValueError(f"endpoint cannot accept experiment inputs: {item.endpoint}")
        spec = specs[item.endpoint.node]
        value = registry[spec.kind].validate_input(spec, item.endpoint.port, item.payload,
            connected_ports=tuple(port.name for port in description.ports
                                  if f"{spec.name}.{port.name}" in connected))
        queued.append(ExperimentInput(item.at_ns, item.endpoint, value))
    consumer = _Consumer(admitted, scene.duration_ns, snapshots, observations)

    def observe(event):
        consumer(event)
        if on_event is not None:
            on_event(event)

    # Reserve a fresh private name without restricting user component names.
    name = "_experiment_cut"
    while name in specs or name in registry:
        name += "_"
    registry[name] = _CutObserverFactory(consumer)
    topology = Topology((*scene.topology.nodes, NodeSpec(name, name)), scene.topology.links)
    with Simulation(topology, registry, quantum_ns=scene.quantum_ns,
                    startup_timeout_s=startup_timeout_s, host_timeout_s=host_timeout_s,
                    max_pending_inputs=max(256, len(queued)), on_event=observe) as simulation:
        for item in queued:
            simulation.schedule_input(item.endpoint, item.payload, at_ns=item.at_ns)
        result = simulation.run_until(scene.duration_ns)
        if result.status != "ok" or result.reached_time_ns != scene.duration_ns:
            raise RuntimeError("experiment did not complete its requested duration")
        consumer.evaluator.finish(scene.duration_ns)
        completed = simulation.t_sim_ns
        total = simulation.events_total
    return ExperimentResult(consumer.evaluator.status, consumer.evaluator.failures,
                            tuple(consumer.matches), completed, consumer.delivered_inputs, total)
