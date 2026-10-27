"""Finite experiments over sim/v1, driven exclusively by Simulation.

Inputs use the runtime's queue and cuts. A resource-free sample consumer marks
already completed cuts for assertion expiry; it never advances CPU or physics.
Matching reuses the pure Lab evaluator, without importing a legacy runner.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
from time import monotonic

from sim.api import (BackendUnavailable, Endpoint, Event, NodeDescription, NodeSpec,
                     PortSpec, TimeQuality, Topology)
from sim.api.events import check_timestamp
from sim.boards import digital_level
from sim.config.scene import ExperimentInput, ExperimentReady, Scene, load_scene
from sim.fabric import CanFrame
from sim.gpio import validate_gpio_pin
from sim.lab.experiment import AssertionEvaluator, AssertionObservation
from sim.lab.manifest import MAX_ITEMS, parse_assertion
from sim.runtime import Simulation, SimulationState


@dataclass(frozen=True)
class ObservationReceipt:
    """The matching public Event's timestamps, unchanged and explicitly named."""

    assertion_index: int | None
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
    origin_ns: int = 0
    ready_receipt: ObservationReceipt | None = None

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
    def __init__(self, assertions, duration_ns, snapshot_endpoints, observations, *,
                 ready=None, on_ready=None):
        self.assertions, self.duration_ns = assertions, duration_ns
        self.ready, self.on_ready = ready, on_ready
        self.origin_ns = None if ready is not None else 0
        self.evaluator = None if ready is not None else AssertionEvaluator(assertions, duration_ns, 0)
        self.ready_receipt = None
        self._ready_tail = b""
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
        if (self.evaluator is not None and self.cut is not None and cut > self.cut
                and self.cut >= self.origin_ns):
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
        if self.evaluator is None:
            self._observe_ready(event, endpoint)
            return
        # The whole matching cut belongs to startup, including other producers
        # and events after the marker in that same observation batch.
        if self.ready is not None and event.delivery_time_ns <= self.origin_ns:
            return
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

    def _observe_ready(self, event, endpoint):
        if event.kind not in ("uart", "net.uart.delivery"):
            return
        data = event.payload
        if event.kind == "net.uart.delivery":
            endpoint, data = event.payload["source"], event.payload["data"]
        wanted = str(self.ready.endpoint)
        if endpoint != wanted or self.observations.get(endpoint) != event.kind:
            return
        if (type(data) is not bytes or event.quality == TimeQuality.LOWER_BOUND
                or event.mapped_time_ns > event.delivery_time_ns):
            raise ValueError("UART ready requires bytes with an observation upper bound")
        combined = self._ready_tail + data
        if self.ready.text not in combined:
            keep = len(self.ready.text) - 1
            self._ready_tail = combined[-keep:] if keep else b""
            return
        origin = event.delivery_time_ns
        check_timestamp(origin + self.duration_ns)
        self.evaluator = AssertionEvaluator(self.assertions, self.duration_ns, origin)
        self.origin_ns = origin
        self._ready_tail = b""
        self.ready_receipt = ObservationReceipt(None, event.session_id, event.sequence,
            event.source_node, event.source_port, event.kind, event.source_time_ns,
            event.mapped_time_ns, event.delivery_time_ns, event.quality)
        if self.on_ready is not None:
            self.on_ready(origin)


class _CutObserver:
    def __init__(self, consumer, on_cut=None):
        self.consumer = consumer
        self.on_cut = on_cut

    def sample(self, cut):
        self.consumer.begin_cut(cut)
        if self.on_cut is not None:
            self.on_cut(cut)

    def close(self):
        pass


class _CutObserverFactory:
    def __init__(self, consumer, on_cut=None):
        self.consumer = consumer
        self.on_cut = on_cut

    def describe(self, _spec):
        return NodeDescription(capabilities={"sample"})

    def create(self, _spec, _context):
        return _CutObserver(self.consumer, self.on_cut)


@dataclass(frozen=True)
class _Admission:
    inputs: tuple
    assertions: tuple
    snapshots: dict
    observations: dict
    descriptions: Mapping


def validate_input_batches(scene, registry, inputs, *, origin_ns):
    """Borrow optional factory admission for interactions within one input cut."""
    for spec in scene.topology.nodes:
        entries = tuple((item.at_ns, item.endpoint.port, item.payload)
                        for item in inputs if item.endpoint.node == spec.name)
        validate = getattr(registry[spec.kind], "validate_inputs", None)
        if entries and validate is not None:
            if not callable(validate):
                raise TypeError(f"factory {spec.kind}.validate_inputs must be callable")
            validate(spec, entries, origin_ns=origin_ns)


def _admit_observations(duration_ns, descriptions, connected, assertions, ready):
    """Admit only public observation capabilities, without factories or files."""
    check_timestamp(duration_ns)
    if duration_ns == 0:
        raise ValueError("experiment scene duration must be positive")
    if not isinstance(assertions, (tuple, list)) or len(assertions) > MAX_ITEMS:
        raise ValueError("experiment requires at most 1024 assertion mappings")
    if ready is not None and type(ready) is not ExperimentReady:
        raise TypeError("experiment ready requires an ExperimentReady")
    snapshots = {}
    observations = {}

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

    if ready is not None:
        assertion_endpoint("uart_contains", str(ready.endpoint))
    admitted = tuple(parse_assertion(_plain(value), index, endpoint_validator=assertion_endpoint)
                     for index, value in enumerate(assertions))
    if any(item.before_ns > duration_ns for item in admitted):
        raise ValueError("assertion deadline exceeds scene duration")
    return admitted, snapshots, observations


def admit_experiment(scene, registry, *, inputs=(), assertions=(), ready=None,
                     descriptions=None):
    """Pure shared config/Python admission; never acquire component resources."""
    if not isinstance(scene, Scene):
        raise TypeError("experiment requires a sim/v1 Scene")
    if len(scene.topology.nodes) > 63:
        raise ValueError("experiment scene requires at most 63 nodes")
    if scene.mode != "stepped":
        raise ValueError("finite experiment requires stepped scene mode")
    if (not isinstance(inputs, (tuple, list)) or len(inputs) > MAX_ITEMS
            or any(type(item) is not ExperimentInput for item in inputs)):
        raise ValueError("experiment requires bounded typed inputs and assertion mappings")
    if descriptions is None:
        descriptions = Simulation(scene.topology, registry, quantum_ns=scene.quantum_ns).validate()
    connected = {str(endpoint) for link in scene.topology.links
                 for endpoint in (link.source, link.target)}
    admitted, snapshots, observations = _admit_observations(
        scene.duration_ns, descriptions, connected, assertions, ready)
    specs = {node.name: node for node in scene.topology.nodes}
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
    validate_input_batches(scene, registry, queued, origin_ns=0 if ready is None else None)
    return _Admission(tuple(queued), admitted, snapshots, observations, descriptions)


def _trace_api():
    # Trace is optional; ordinary experiments never import its MCAP dependency.
    try:
        from sim.trace.experiment import ExperimentTrace, iter_experiment_records
    except ModuleNotFoundError as error:
        if error.name == "mcap" or (error.name or "").startswith("mcap."):
            raise BackendUnavailable("experiment trace requires the trace extra") from error
        raise
    return ExperimentTrace, iter_experiment_records


def _trace_config(scene, descriptions, assertions, ready):
    return {
        "duration_ns": scene.duration_ns,
        "quantum_ns": scene.quantum_ns,
        "ready": None if ready is None else {"endpoint": str(ready.endpoint), "text": ready.text},
        "assertions": tuple(_plain(value) for value in assertions),
        # Preserve all declared observation capabilities for replacement
        # assertions, including snapshot-backed GPIOs not named in this run.
        "descriptions": {name: {
            "capabilities": tuple(sorted(description.capabilities)),
            "ports": tuple({"name": port.name, "protocol": port.protocol,
                            "direction": port.direction} for port in description.ports),
        } for name, description in descriptions.items()},
        "connected": tuple(sorted({str(endpoint) for link in scene.topology.links
                                    for endpoint in (link.source, link.target)})),
    }


def run_experiment(scene: Scene | str | Path, registry, *, inputs=(), assertions=(), ready=None,
                   startup_timeout_s=10, host_timeout_s=30, on_event=None,
                   trace=None) -> ExperimentResult:
    """Run relative inputs/assertions on one Simulation and clean before return.

    An optional actual UART ready marker establishes the experiment origin at
    its delivery cut. Without ready the origin is zero. Neither path rewrites
    Session, Event or native timestamps. Matching ignores the whole ready cut.
    Machine UART/CAN use public observations; other connected producers use
    successful delivery events, keeping transport admission separate from reads.
    Optional trace borrows a blocking binary output. Events and actual sample
    cuts are recorded in callback order; completion/flush follows cleanup, even
    for false assertions. Runtime or cleanup failures never publish completion.
    Recording failures return no result and preserve accepted bytes, which may
    include completion if a final flush fails. The caller alone closes output.
    """
    trace_type = _trace_api()[0] if trace is not None else None
    registry = dict(registry)
    scene = load_scene(scene, registry) if isinstance(scene, (str, Path)) else scene
    if not isinstance(scene, Scene):
        raise TypeError("experiment requires a sim/v1 Scene")
    if scene.experiment is not None:
        if inputs or assertions or ready is not None:
            raise ValueError("scene experiment cannot be overridden by Python experiment values")
        inputs, assertions, ready = (scene.experiment.inputs, scene.experiment.assertions,
                                     scene.experiment.ready)
    if on_event is not None and not callable(on_event):
        raise TypeError("experiment on_event must be callable")
    admitted = admit_experiment(scene, registry, inputs=inputs, assertions=assertions, ready=ready)
    recorder = None
    simulation = None
    scheduled = False
    cuts_total = 0

    def enqueue(origin):
        nonlocal scheduled
        validate_input_batches(scene, registry, admitted.inputs, origin_ns=origin)
        # start hooks/initial sampling run while Simulation is NEW. They can
        # establish readiness, but enqueue only after all construction completes.
        if simulation.state == SimulationState.NEW:
            return
        for item in admitted.inputs:
            simulation.schedule_input(item.endpoint, item.payload, at_ns=origin + item.at_ns)
        scheduled = True

    consumer = _Consumer(admitted.assertions, scene.duration_ns, admitted.snapshots,
                         admitted.observations, ready=ready, on_ready=enqueue)

    def observe(event):
        if recorder is not None:
            recorder.event(event)
        consumer(event)
        if on_event is not None:
            on_event(event)

    def record_cut(cut):
        nonlocal cuts_total
        recorder.cut(cut)
        cuts_total += 1

    # Reserve a fresh private name without restricting user component names.
    name = "_experiment_cut"
    while name in {node.name for node in scene.topology.nodes} or name in registry:
        name += "_"
    registry[name] = _CutObserverFactory(consumer, None if trace is None else record_cut)
    topology = Topology((*scene.topology.nodes, NodeSpec(name, name)), scene.topology.links)
    simulation = Simulation(topology, registry, quantum_ns=scene.quantum_ns,
                            startup_timeout_s=startup_timeout_s, host_timeout_s=host_timeout_s,
                            max_pending_inputs=max(256, len(admitted.inputs)), on_event=observe)
    if trace is not None:
        recorder = trace_type(trace, _trace_config(scene, admitted.descriptions, assertions, ready))
    begun = monotonic()
    total_deadline = begun + host_timeout_s
    startup_deadline = min(total_deadline, begun + startup_timeout_s)
    primary = None
    try:
        simulation.start(deadline_monotonic=startup_deadline)
        if consumer.origin_ns is not None and not scheduled:
            enqueue(consumer.origin_ns)
        while consumer.origin_ns is None:
            target = simulation.t_sim_ns + scene.quantum_ns
            check_timestamp(target)
            simulation.run_until(target, deadline_monotonic=startup_deadline)
        end = consumer.origin_ns + scene.duration_ns
        check_timestamp(end)
        result = simulation.run_until(end, deadline_monotonic=total_deadline)
        if result.status != "ok" or result.reached_time_ns != end:
            raise RuntimeError("experiment did not complete its requested duration")
        consumer.evaluator.finish(end)
        completed = simulation.t_sim_ns
        total = simulation.events_total
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            simulation.close()
        except BaseException as cleanup:
            if primary is None:
                raise
            if cleanup is not primary:
                primary.add_note(f"simulation cleanup failed: {type(cleanup).__name__}: {cleanup}")
                for note in getattr(cleanup, "__notes__", ()):
                    primary.add_note(note)
    result = ExperimentResult(consumer.evaluator.status, consumer.evaluator.failures,
                              tuple(consumer.matches), completed, consumer.delivered_inputs, total,
                              consumer.origin_ns, consumer.ready_receipt)
    if recorder is not None:
        recorder.complete({"end_ns": completed, "origin_ns": consumer.origin_ns,
                           "delivered_inputs": consumer.delivered_inputs,
                           "events_total": total, "cuts_total": cuts_total})
    return result


def _exact_mapping(value, keys, label):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"invalid experiment trace {label}")
    return value


@dataclass(frozen=True)
class _ReplayPlan:
    duration_ns: int
    quantum_ns: int
    ready: ExperimentReady | None
    assertions: tuple
    snapshots: dict
    observations: dict
    descriptions: dict


def _replay_plan(config, assertions):
    config = _exact_mapping(config, ("duration_ns", "quantum_ns", "ready", "assertions",
                                     "descriptions", "connected"), "configuration")
    duration, quantum = config["duration_ns"], config["quantum_ns"]
    for value in (duration, quantum):
        check_timestamp(value)
        if not value:
            raise ValueError("experiment trace duration and quantum must be positive")
    raw = config["descriptions"]
    if not isinstance(raw, Mapping) or not 1 <= len(raw) <= 63:
        raise ValueError("experiment trace requires 1..63 node descriptions")
    descriptions = {}
    for name, description in raw.items():
        NodeSpec(name, "recorded")  # Reuse public identifier admission, without a factory.
        description = _exact_mapping(description, ("capabilities", "ports"), "node description")
        capabilities, ports = description["capabilities"], description["ports"]
        if (type(capabilities) is not tuple or len(set(capabilities)) != len(capabilities)
                or type(ports) is not tuple):
            raise ValueError("experiment trace descriptions require capability and port tuples")
        admitted_ports = []
        for port in ports:
            port = _exact_mapping(port, ("name", "protocol", "direction"), "port description")
            admitted_ports.append(PortSpec(**port))
        descriptions[name] = NodeDescription(tuple(admitted_ports), capabilities)
    connected = config["connected"]
    if (type(connected) is not tuple or len(connected) > 512
            or len(set(connected)) != len(connected)):
        raise ValueError("experiment trace requires bounded unique connected endpoints")
    for value in connected:
        endpoint = Endpoint.parse(value)
        if endpoint.node not in descriptions:
            raise ValueError("experiment trace connection names an unknown node")
        descriptions[endpoint.node].port(endpoint.port)
    ready = config["ready"]
    if ready is not None:
        ready = _exact_mapping(ready, ("endpoint", "text"), "ready declaration")
        ready = ExperimentReady(ready["endpoint"], ready["text"])
    original = _admit_observations(duration, descriptions, connected, config["assertions"], ready)
    admitted, snapshots, observations = (original if assertions is None else
        _admit_observations(duration, descriptions, connected, assertions, ready))
    return _ReplayPlan(duration, quantum, ready, admitted, snapshots, observations, descriptions)


class _DigestInput:
    """Hash exactly the bytes consumed by the canonical bounded MCAP reader."""

    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.source.read(size)
        if type(data) is bytes:
            self.digest.update(data)
        return data


def _replay_pass(source, records, assertions, expected_config=None):
    config = plan = consumer = summary = None
    previous_cut = pending_cut = None
    cuts_total = 0
    for record in records(source):
        if record.kind == "config":
            if config is not None:
                raise ValueError("duplicate experiment trace configuration")
            config = record.data
            if expected_config is not None and config != expected_config:
                raise ValueError("experiment trace configuration changed between passes")
            plan = _replay_plan(config, assertions)
            consumer = _Consumer(plan.assertions, plan.duration_ns, plan.snapshots,
                                 plan.observations, ready=plan.ready)
            continue
        if plan is None or summary is not None:
            raise ValueError("invalid experiment trace lifecycle order")
        if record.kind == "event":
            event = record.data
            if event.source_node not in plan.descriptions:
                raise ValueError("experiment trace Event names an unknown node")
            if event.source_port is not None:
                plan.descriptions[event.source_node].port(event.source_port)
            if previous_cut is None or event.delivery_time_ns > previous_cut:
                if pending_cut is not None and pending_cut != event.delivery_time_ns:
                    raise ValueError("experiment trace lacks an observation cut")
                pending_cut = event.delivery_time_ns
            consumer(event)
        elif record.kind == "cut":
            cut = record.data
            if ((previous_cut is None and cut != 0) or
                    (previous_cut is not None and not 0 <= cut - previous_cut <= plan.quantum_ns) or
                    (pending_cut is not None and pending_cut != cut)):
                raise ValueError("experiment trace observation cuts differ from its runtime")
            consumer.begin_cut(cut)
            previous_cut, pending_cut = cut, None
            cuts_total += 1
        elif record.kind == "completion":
            summary = _exact_mapping(record.data,
                ("end_ns", "origin_ns", "delivered_inputs", "events_total", "cuts_total"),
                "completion")
        else:
            raise ValueError("unsupported experiment trace record")
    # The iterator has now checked every CRC, footer, trailing magic and EOF.
    if summary is None or consumer is None or consumer.evaluator is None:
        raise ValueError("experiment trace lacks its completed ready lifecycle")
    for value in summary.values():
        check_timestamp(value)
    end = summary["end_ns"]
    if (pending_cut is not None or previous_cut != end or
            consumer.origin_ns != summary["origin_ns"] or
            end != consumer.origin_ns + plan.duration_ns or
            consumer.delivered_inputs != summary["delivered_inputs"] or
            consumer.sequence + 1 != summary["events_total"] or
            cuts_total != summary["cuts_total"]):
        raise ValueError("experiment trace completion differs from its captured lifecycle")
    consumer.evaluator.finish(end)
    result = ExperimentResult(consumer.evaluator.status, consumer.evaluator.failures,
        tuple(consumer.matches), end, consumer.delivered_inputs, consumer.sequence + 1,
        consumer.origin_ns, consumer.ready_receipt)
    return config, result


def replay_experiment(source, assertions=None) -> ExperimentResult:
    """Reevaluate a complete finite transcript without factories or native IO.

    Default assertions are the recorded originals. Replacement mappings use the
    same grammar and all captured public capabilities. The borrowed blocking
    read/seek/tell stream must start at zero and remain unchanged. Two complete
    container/lifecycle passes and equal SHA256 digests precede any result.
    Successful calls leave EOF; errors neither retry, rewind again nor close.
    """
    _, records = _trace_api()
    if any(not callable(getattr(source, name, None)) for name in ("read", "seek", "tell")):
        raise TypeError("experiment replay input requires read/seek/tell")
    position = source.tell()
    if type(position) is not int or position != 0:
        raise ValueError("experiment replay input must start at byte zero")
    if assertions is not None and (not isinstance(assertions, (tuple, list)) or
                                   len(assertions) > MAX_ITEMS):
        raise ValueError("experiment requires at most 1024 assertion mappings")
    first = _DigestInput(source)
    config, _ = _replay_pass(first, records, assertions)
    source.seek(0)
    position = source.tell()
    if type(position) is not int or position != 0:
        raise ValueError("experiment replay could not rewind to byte zero")
    second = _DigestInput(source)
    _, result = _replay_pass(second, records, assertions, expected_config=config)
    if first.digest.digest() != second.digest.digest():
        raise ValueError("experiment trace bytes changed between passes")
    return result
