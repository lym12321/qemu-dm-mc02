"""Finite experiments over sim/v1, driven exclusively by Simulation.

Inputs use the runtime's queue and cuts. A resource-free sample consumer marks
already completed cuts for assertion expiry; it never advances CPU or physics.
Matching reuses the pure Lab evaluator, without importing a legacy runner.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from time import monotonic

from sim.api import (BackendUnavailable, Endpoint, Event, NodeDescription, NodeSpec,
                     PortSpec, TimeQuality, Topology)
from sim.api.events import check_timestamp
from sim.boards import digital_level
from sim.config.scene import ExperimentInput, ExperimentReady, Scene, load_scene, validate_ready
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
    ready_receipts: dict[str, ObservationReceipt] = field(default_factory=dict)
    boards: dict[str, dict] = field(default_factory=dict)

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
                 ready=(), assertion_origins=None, on_unlock=None, machine_nodes=()):
        self.assertions, self.duration_ns = assertions, duration_ns
        self.ready = validate_ready(ready)
        self.on_unlock = on_unlock
        self.origin_ns = None if self.ready else 0
        self.ready_receipts = {}
        self._ready_tails = {item.name: b"" for item in self.ready}
        self.snapshot_endpoints, self.observations = snapshot_endpoints, observations
        self.machine_nodes, self.boards = frozenset(machine_nodes), {}
        self.assertion_origins = tuple(assertion_origins or ("ready",) * len(assertions))
        self._evaluators = {}
        self._start_evaluator("session", 0)
        if not self.ready:
            self._start_evaluator("ready", 0)
        self.cut = None
        self.sequence = -1
        self.matches = []
        self.delivered_inputs = 0

    def _start_evaluator(self, origin, at):
        indices = tuple(index for index, value in enumerate(self.assertion_origins) if value == origin)
        self._evaluators[origin] = (indices, AssertionEvaluator(
            tuple(self.assertions[index] for index in indices), self.duration_ns, at))

    @property
    def status(self):
        result = ["pending"] * len(self.assertions)
        for indices, evaluator in self._evaluators.values():
            for index, status in zip(indices, evaluator.status):
                result[index] = status
        return tuple(result)

    @property
    def failures(self):
        return tuple(f"assertion[{index + 1}] {item.kind} {item.endpoint} "
                     f"did not match before {item.before_ns} ns"
                     for index, (item, status) in enumerate(zip(self.assertions, self.status))
                     if status == "fail")

    def finish(self, end):
        if self.origin_ns is None:
            raise ValueError("experiment lacks its completed ready lifecycle")
        for _, evaluator in self._evaluators.values():
            evaluator.finish(end)

    def begin_cut(self, cut):
        check_timestamp(cut)
        if self.cut is not None and cut < self.cut:
            raise ValueError("experiment observation cuts moved backwards")
        if self.cut is not None and cut > self.cut:
            for _, evaluator in self._evaluators.values():
                if self.cut >= evaluator.origin_ns:
                    evaluator.advance(self.cut)
        self.cut = cut

    @staticmethod
    def _receipt(event, index=None):
        return ObservationReceipt(index, event.session_id, event.sequence,
            event.source_node, event.source_port, event.kind, event.source_time_ns,
            event.mapped_time_ns, event.delivery_time_ns, event.quality)

    def __call__(self, event):
        if not isinstance(event, Event) or event.session_id != 1 or event.sequence != self.sequence + 1:
            raise ValueError("experiment requires ordered Events from its one Simulation session")
        self.begin_cut(event.delivery_time_ns)
        self.sequence = event.sequence
        if event.kind == "input.delivered":
            self.delivered_inputs += 1
        self._board_observation(event)
        endpoint = (f"{event.source_node}.{event.source_port}"
                    if event.source_port is not None else None)
        self._observe_ready(event, endpoint)
        digital, observed = self._values(event, endpoint)
        if not digital and not observed:
            return
        matches = []
        for origin, (indices, evaluator) in self._evaluators.items():
            if origin == "ready" and self.ready and event.delivery_time_ns <= self.origin_ns:
                continue
            before = evaluator.status
            evaluator.observe_values(digital, observed, event.delivery_time_ns, expire=False)
            for index, old, new in zip(indices, before, evaluator.status):
                if old == "pending" and new == "pass":
                    matches.append(self._receipt(event, index))
        self.matches.extend(sorted(matches, key=lambda receipt: receipt.assertion_index))

    def _board_observation(self, event):
        if event.kind != "snapshot" or event.source_node not in self.machine_nodes:
            return
        native = event.source_time_ns
        if (type(native) is not int or not isinstance(event.payload, Mapping)
                or event.payload.get("timestamp_ns") != native
                or event.mapped_time_ns != event.delivery_time_ns or native < event.mapped_time_ns):
            raise ValueError("machine snapshot must preserve its native epoch and completed cut")
        epoch = native - event.mapped_time_ns
        old = self.boards.get(event.source_node)
        if old is not None and (old["native_origin_ns"] != epoch or native < old["last_source_ns"]):
            raise ValueError("machine snapshot native epoch changed or moved backwards")
        self.boards[event.source_node] = {
            "native_origin_ns": epoch,
            "first_source_ns": native if old is None else old["first_source_ns"],
            "last_source_ns": native,
            "first_mapped_ns": event.mapped_time_ns if old is None else old["first_mapped_ns"],
            "last_mapped_ns": event.mapped_time_ns,
            "observation_batches": 1 if old is None else old["observation_batches"] + 1,
        }

    def _values(self, event, endpoint):
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
                return {}, ()
            if (endpoint is None or type(data) is not bytes
                    or event.quality == TimeQuality.LOWER_BOUND):
                raise ValueError("UART assertion requires bytes with an observation upper bound")
            observed = (AssertionObservation(endpoint, event.mapped_time_ns, data),)
        elif event.kind in ("can-tx", "can.delivery"):
            if self.observations.get(endpoint) != event.kind:
                return {}, ()
            if event.kind == "can.delivery" and event.payload["dropped"]:
                return {}, ()
            if event.quality == TimeQuality.LOWER_BOUND:
                raise ValueError("CAN assertion requires an observation upper bound")
            frame = event.payload if event.kind == "can-tx" else event.payload.get("frame")
            if not isinstance(frame, CanFrame):
                raise TypeError("CAN observation requires the actual CanFrame")
            observed = (AssertionObservation(endpoint, event.mapped_time_ns, frame),)
        elif event.kind in ("digital", "net.digital.sample") and endpoint is not None:
            if self.observations.get(endpoint) != "digital":
                return {}, ()
            if event.mapped_time_ns != event.delivery_time_ns:
                raise ValueError("digital sample requires its completed observation cut")
            level = event.payload if event.kind == "digital" else event.payload["level"]
            if level is not None:
                digital[endpoint] = level
        return digital, observed

    def _observe_ready(self, event, endpoint):
        if self.origin_ns is not None or event.kind not in ("uart", "net.uart.delivery"):
            return
        data = event.payload
        if event.kind == "net.uart.delivery":
            endpoint, data = event.payload["source"], event.payload["data"]
        matched = []
        for item in self.ready:
            if (item.name in self.ready_receipts or endpoint != str(item.endpoint)
                    or self.observations.get(endpoint) != event.kind):
                continue
            if (type(data) is not bytes or event.quality == TimeQuality.LOWER_BOUND
                    or event.mapped_time_ns > event.delivery_time_ns):
                raise ValueError("UART ready requires bytes with an observation upper bound")
            combined = self._ready_tails[item.name] + data
            if item.text not in combined:
                keep = len(item.text) - 1
                self._ready_tails[item.name] = combined[-keep:] if keep else b""
                continue
            self._ready_tails[item.name] = b""
            self.ready_receipts[item.name] = self._receipt(event)
            matched.append(item.name)
        if len(self.ready_receipts) == len(self.ready):
            origin = event.delivery_time_ns
            check_timestamp(origin + self.duration_ns)
            self.origin_ns = origin
            self._start_evaluator("ready", origin)
        if matched and self.on_unlock is not None:
            self.on_unlock()


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
    assertion_origins: tuple


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
    ready = validate_ready(ready)
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

    for item in ready:
        assertion_endpoint("uart_contains", str(item.endpoint))
    admitted, origins = [], []
    for index, value in enumerate(assertions):
        value = _plain(value)
        if not isinstance(value, dict):
            raise TypeError("experiment assertions require mappings")
        origin = value.pop("origin", "ready")
        if origin not in ("session", "ready"):
            raise ValueError("assertion origin must be session or ready")
        origins.append(origin)
        admitted.append(parse_assertion(value, index, endpoint_validator=assertion_endpoint))
    admitted = tuple(admitted)
    if any(item.before_ns > duration_ns for item in admitted):
        raise ValueError("assertion deadline exceeds scene duration")
    return admitted, snapshots, observations, tuple(origins)


def admit_experiment(scene, registry, *, inputs=(), assertions=(), ready=(),
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
    ready = validate_ready(ready)
    admitted, snapshots, observations, origins = _admit_observations(
        scene.duration_ns, descriptions, connected, assertions, ready)
    specs = {node.name: node for node in scene.topology.nodes}
    queued = []
    marker_origins = {"marker:" + item.name for item in ready}
    for item in inputs:
        if item.origin.startswith("marker:") and item.origin not in marker_origins:
            raise ValueError("experiment input names an unknown ready marker")
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
        queued.append(ExperimentInput(item.at_ns, item.endpoint, value, item.origin))
    if not ready:
        validate_input_batches(scene, registry, queued, origin_ns=0)
    else:
        for origin in dict.fromkeys(item.origin for item in queued):
            validate_input_batches(scene, registry, tuple(item for item in queued if item.origin == origin),
                                  origin_ns=0 if origin == "session" else None)
    return _Admission(tuple(queued), admitted, snapshots, observations, descriptions, origins)


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
        "ready": tuple({"name": item.name, "endpoint": str(item.endpoint), "text": item.text}
                       for item in ready),
        "assertions": tuple(_plain(value) for value in assertions),
        # Preserve all declared observation capabilities for replacement
        # assertions, including snapshot-backed GPIOs not named in this run.
        "descriptions": {name: {
            "capabilities": tuple(sorted(description.capabilities)),
            "ports": tuple({"name": port.name, "protocol": port.protocol,
                            "direction": port.direction,
                            "capabilities": tuple(sorted(port.capabilities))}
                           for port in description.ports),
        } for name, description in descriptions.items()},
        "connected": tuple(sorted({str(endpoint) for link in scene.topology.links
                                    for endpoint in (link.source, link.target)})),
    }


def _receipt_value(receipt):
    return {"assertion_index": receipt.assertion_index, "session_id": receipt.session_id,
            "event_sequence": receipt.event_sequence, "source_node": receipt.source_node,
            "source_port": receipt.source_port, "kind": receipt.kind,
            "source_time_ns": receipt.source_time_ns, "mapped_time_ns": receipt.mapped_time_ns,
            "delivery_time_ns": receipt.delivery_time_ns, "quality": str(receipt.quality)}


def _ready_values(consumer):
    return {item.name: _receipt_value(consumer.ready_receipts[item.name]) for item in consumer.ready}


def _result(consumer, end, total):
    return ExperimentResult(consumer.status, consumer.failures, tuple(consumer.matches), end,
        consumer.delivered_inputs, total, consumer.origin_ns,
        {item.name: consumer.ready_receipts[item.name] for item in consumer.ready},
        dict(consumer.boards))


def run_experiment(scene: Scene | str | Path, registry, *, inputs=(), assertions=(), ready=(),
                   startup_timeout_s=10, host_timeout_s=30, on_event=None,
                   on_cut=None, trace=None) -> ExperimentResult:
    """Run one finite Simulation; readiness never changes native/session epochs.

    Named UART markers unlock their own input groups immediately. All markers
    establish the active origin at the final matching cut. Session assertions
    include startup; ready assertions exclude the entire matching cut. Inputs
    use the sole runtime queue, with session inputs admitted before newly
    unlocked groups and declaration order within each group. Trace completion
    follows all component cleanup, including for false assertions.
    on_cut(cut_ns, simulation) borrows the existing source-sample cut before target delivery; it
    must not reenter execution or claim that target input commit has succeeded.
    """
    trace_type = _trace_api()[0] if trace is not None else None
    registry = dict(registry)
    scene = load_scene(scene, registry) if isinstance(scene, (str, Path)) else scene
    if not isinstance(scene, Scene):
        raise TypeError("experiment requires a sim/v1 Scene")
    if scene.experiment is not None:
        if inputs or assertions or ready:
            raise ValueError("scene experiment cannot be overridden by Python experiment values")
        inputs, assertions, ready = (scene.experiment.inputs, scene.experiment.assertions,
                                     scene.experiment.ready)
    ready = validate_ready(ready)
    for callback, label in ((on_event, "on_event"), (on_cut, "on_cut")):
        if callback is not None and not callable(callback):
            raise TypeError(f"experiment {label} must be callable")
    admitted = admit_experiment(scene, registry, inputs=inputs, assertions=assertions, ready=ready)
    recorder = simulation = None
    scheduled = set()
    cuts_total = 0
    startup_complete = False

    def input_origin(item):
        if item.origin == "session":
            return 0
        if item.origin == "ready":
            return consumer.origin_ns
        receipt = consumer.ready_receipts.get(item.origin[7:])
        return None if receipt is None else receipt.delivery_time_ns

    def enqueue():
        nonlocal startup_complete
        # Initial source sampling may match markers while construction is NEW.
        # The first public operation settles all deferred inputs before CPU IO.
        if simulation.state == SimulationState.NEW:
            return
        known = tuple((index, item, input_origin(item)) for index, item in enumerate(admitted.inputs)
                      if input_origin(item) is not None)
        absolute = tuple(ExperimentInput(origin + item.at_ns, item.endpoint, item.payload, "session")
                         for _, item, origin in known)
        validate_input_batches(scene, registry, absolute, origin_ns=0)
        order = sorted(known, key=lambda row: (row[1].origin != "session", row[0]))
        for index, item, origin in order:
            if index not in scheduled:
                simulation.schedule_input(item.endpoint, item.payload, at_ns=origin + item.at_ns)
                scheduled.add(index)
        if consumer.origin_ns is not None and not startup_complete:
            startup_complete = True
            # Readiness can arise at an input/deadline cut inside the current
            # startup request. Return at that cut before choosing the active
            # end, which can precede the startup request's original target.
            if simulation.state == SimulationState.RUNNING:
                simulation.pause()

    consumer = _Consumer(admitted.assertions, scene.duration_ns, admitted.snapshots,
        admitted.observations, ready=ready, assertion_origins=admitted.assertion_origins,
        on_unlock=enqueue, machine_nodes=tuple(name for name, description in admitted.descriptions.items()
                                             if "machine" in description.capabilities))

    def observe(event):
        if recorder is not None:
            recorder.event(event)
        consumer(event)
        if on_event is not None:
            on_event(event)

    def sample_cut(cut):
        nonlocal cuts_total
        if recorder is not None:
            recorder.cut(cut)
        cuts_total += 1
        if on_cut is not None:
            on_cut(cut, simulation)

    name = "_experiment_cut"
    while name in {node.name for node in scene.topology.nodes} or name in registry:
        name += "_"
    registry[name] = _CutObserverFactory(consumer, sample_cut)
    topology = Topology((*scene.topology.nodes, NodeSpec(name, name)), scene.topology.links)
    simulation = Simulation(topology, registry, quantum_ns=scene.quantum_ns,
                            startup_timeout_s=startup_timeout_s, host_timeout_s=host_timeout_s,
                            max_pending_inputs=max(256, len(admitted.inputs)), on_event=observe)
    if trace is not None:
        config = _trace_config(scene, admitted.descriptions, assertions, ready)
        config["inputs"] = tuple({"at_ns": item.at_ns, "endpoint": str(item.endpoint),
                                  "payload": item.payload, "origin": item.origin}
                                 for item in admitted.inputs)
        recorder = trace_type(trace, config)
    begun = monotonic()
    total_deadline = begun + host_timeout_s
    startup_deadline = min(total_deadline, begun + startup_timeout_s)
    primary = None
    try:
        simulation.start(deadline_monotonic=startup_deadline)
        enqueue()
        while consumer.origin_ns is None:
            target = simulation.t_sim_ns + scene.quantum_ns
            check_timestamp(target)
            simulation.run_until(target, deadline_monotonic=startup_deadline)
        end = consumer.origin_ns + scene.duration_ns
        check_timestamp(end)
        result = simulation.run_until(end, deadline_monotonic=total_deadline)
        if result.status != "ok" or result.reached_time_ns != end:
            raise RuntimeError("experiment did not complete its requested duration")
        consumer.finish(end)
        completed, total = simulation.t_sim_ns, simulation.events_total
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
    result = _result(consumer, completed, total)
    if recorder is not None:
        recorder.complete({"end_ns": completed, "origin_ns": consumer.origin_ns,
                           "delivered_inputs": consumer.delivered_inputs,
                           "events_total": total, "cuts_total": cuts_total,
                           "ready_receipts": _ready_values(consumer), "boards": dict(consumer.boards)})
    return result


def _exact_mapping(value, keys, label):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"invalid experiment trace {label}")
    return value


@dataclass(frozen=True)
class _ReplayPlan:
    duration_ns: int
    quantum_ns: int
    ready: tuple[ExperimentReady, ...]
    assertions: tuple
    snapshots: dict
    observations: dict
    descriptions: dict
    assertion_origins: tuple


def _replay_plan(config, assertions):
    config = _exact_mapping(config, ("duration_ns", "quantum_ns", "ready", "assertions",
                                     "descriptions", "connected", "inputs"), "configuration")
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
            port = _exact_mapping(port, ("name", "protocol", "direction", "capabilities"),
                                  "port description")
            if (type(port["capabilities"]) is not tuple or
                    len(set(port["capabilities"])) != len(port["capabilities"])):
                raise ValueError("experiment trace port capabilities require unique tuples")
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
    if type(ready) is not tuple:
        raise ValueError("experiment trace ready markers require a tuple")
    ready = validate_ready(tuple(ExperimentReady(**_exact_mapping(
        value, ("name", "endpoint", "text"), "ready declaration")) for value in ready))
    inputs = config["inputs"]
    if type(inputs) is not tuple or len(inputs) > MAX_ITEMS:
        raise ValueError("experiment trace requires bounded input declarations")
    names = {"marker:" + item.name for item in ready}
    for value in inputs:
        value = _exact_mapping(value, ("at_ns", "endpoint", "payload", "origin"), "input declaration")
        item = ExperimentInput(**value)
        if (item.at_ns > duration or item.endpoint.node not in descriptions
                or item.origin.startswith("marker:") and item.origin not in names):
            raise ValueError("invalid recorded input origin, endpoint or horizon")
        description = descriptions[item.endpoint.node]
        port = description.port(item.endpoint.port)
        if ("input" not in description.capabilities or port.direction == "out"
                or "external_input" not in port.capabilities):
            raise ValueError("recorded endpoint cannot accept experiment inputs")
    original = _admit_observations(duration, descriptions, connected, config["assertions"], ready)
    admitted, snapshots, observations, origins = (original if assertions is None else
        _admit_observations(duration, descriptions, connected, assertions, ready))
    return _ReplayPlan(duration, quantum, ready, admitted, snapshots, observations, descriptions, origins)


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
                                 plan.observations, ready=plan.ready, assertion_origins=plan.assertion_origins,
                                 machine_nodes=tuple(name for name, description in plan.descriptions.items()
                                                     if "machine" in description.capabilities))
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
                ("end_ns", "origin_ns", "delivered_inputs", "events_total", "cuts_total",
                 "ready_receipts", "boards"),
                "completion")
        else:
            raise ValueError("unsupported experiment trace record")
    # The iterator has now checked every CRC, footer, trailing magic and EOF.
    if summary is None or consumer is None or consumer.origin_ns is None:
        raise ValueError("experiment trace lacks its completed ready lifecycle")
    for key in ("end_ns", "origin_ns", "delivered_inputs", "events_total", "cuts_total"):
        check_timestamp(summary[key])
    end = summary["end_ns"]
    if (pending_cut is not None or previous_cut != end or
            consumer.origin_ns != summary["origin_ns"] or
            end != consumer.origin_ns + plan.duration_ns or
            consumer.delivered_inputs != summary["delivered_inputs"] or
            consumer.sequence + 1 != summary["events_total"] or
            cuts_total != summary["cuts_total"] or
            _ready_values(consumer) != summary["ready_receipts"] or
            consumer.boards != summary["boards"]):
        raise ValueError("experiment trace completion differs from its captured lifecycle")
    consumer.finish(end)
    result = _result(consumer, end, consumer.sequence + 1)
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
