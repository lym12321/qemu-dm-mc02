"""The single public scene schema, independent of registered implementations."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from sim.api import Endpoint, LinkSpec, NodeSpec
from sim.api.events import check_timestamp
from sim.api.topology import immutable
from sim.runtime import Simulation, Topology

from .yaml import ManifestError, fields, identifier, load_document, parse_time, text_bytes


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
class ExperimentReady:
    """An actual UART observation requested before relative experiment time."""

    endpoint: Endpoint | str
    text: bytes | str

    def __post_init__(self):
        object.__setattr__(self, "endpoint", Endpoint.parse(self.endpoint))
        value = self.text
        if type(value) is str:
            value = text_bytes(value, "experiment.ready.text")
        if type(value) is not bytes or not 1 <= len(value) <= 4096:
            raise ValueError("experiment.ready.text requires 1..4096 UTF-8 bytes")
        value.decode("utf-8")
        object.__setattr__(self, "text", value)


@dataclass(frozen=True)
class ExperimentSpec:
    ready: ExperimentReady | None = None
    inputs: tuple[ExperimentInput, ...] = ()
    assertions: tuple[object, ...] = ()

    def __post_init__(self):
        if self.ready is not None and type(self.ready) is not ExperimentReady:
            raise TypeError("experiment.ready requires an ExperimentReady")
        for name in ("inputs", "assertions"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or len(values) > 1024:
                raise ValueError(f"experiment.{name} requires at most 1024 entries")
            object.__setattr__(self, name, tuple(immutable(value) for value in values))


@dataclass(frozen=True)
class Scene:
    topology: Topology
    mode: str = "stepped"
    quantum_ns: int = 1_000_000
    duration_ns: int = 200_000_000
    experiment: ExperimentSpec | None = None


def load_scene(path: str | Path, registry: Mapping) -> Scene:
    """Read, describe and validate every component without constructing one."""
    source, document = load_document(path)
    fields(document, {"schema", "runtime", "nodes", "links", "experiment"},
           {"schema", "nodes"}, "scene")
    if document["schema"] != "sim/v1":
        raise ManifestError("scene.schema must be sim/v1")
    runtime = fields(document.get("runtime", {}), {"mode", "quantum", "duration"},
                     set(), "runtime")
    mode = runtime.get("mode", "stepped")
    if mode not in ("stepped", "interactive"):
        raise ManifestError("runtime.mode must be stepped or interactive")
    quantum = parse_time(runtime.get("quantum", "1ms"), "runtime.quantum", zero=False)
    duration = parse_time(runtime.get("duration", "200ms"), "runtime.duration", zero=False)
    raw_nodes = document["nodes"]
    if type(raw_nodes) is not dict or not 1 <= len(raw_nodes) <= 64:
        raise ManifestError("nodes must contain 1..64 named components")
    nodes = []
    for name, options in raw_nodes.items():
        identifier(name, "node name")
        if type(options) is not dict or "kind" not in options:
            raise ManifestError(f"node {name} requires a kind")
        kind = options["kind"]
        text_bytes(kind, f"node {name}.kind", maximum=128)
        if kind not in registry:
            raise ManifestError(f"node {name}: unregistered kind {kind!r}")
        nodes.append(NodeSpec(name, kind, {key: value for key, value in options.items()
                                          if key != "kind"}, base_dir=source.parent))
    raw_links = document.get("links", [])
    if type(raw_links) is not list or len(raw_links) > 256:
        raise ManifestError("links must be a list of at most 256 endpoint pairs")
    links = []
    for pair in raw_links:
        if type(pair) is not list or len(pair) != 2:
            raise ManifestError("each link must be a pair of qualified endpoints")
        for endpoint in pair:
            text_bytes(endpoint, "link endpoint", maximum=256)
            if "\0" in endpoint:
                raise ManifestError("link endpoint must not contain NUL")
        links.append(LinkSpec(*pair))
    topology = Topology(tuple(nodes), tuple(links))
    # Constructor admission also verifies which typed connections the first
    # runtime actually executes; describe/create are deliberately separate.
    descriptions = Simulation(topology, registry, mode=mode, quantum_ns=quantum).validate()
    scene = Scene(topology, mode, quantum, duration)
    if "experiment" not in document:
        return scene
    experiment = fields(document["experiment"], {"ready", "inputs", "assertions"},
                        set(), "experiment")
    ready = None
    if "ready" in experiment:
        item = fields(experiment["ready"], {"endpoint", "text"},
                      {"endpoint", "text"}, "experiment.ready")
        ready = ExperimentReady(item["endpoint"], text_bytes(item["text"], "experiment.ready.text"))
    inputs = experiment.get("inputs", [])
    assertions = experiment.get("assertions", [])
    for label, values in (("inputs", inputs), ("assertions", assertions)):
        if type(values) is not list or len(values) > 1024:
            raise ManifestError(f"experiment.{label} must be a list with at most 1024 entries")
    queued = []
    for index, value in enumerate(inputs):
        label = f"experiment.inputs[{index}]"
        item = fields(value, {"at", "endpoint", "value"}, {"at", "endpoint", "value"}, label)
        queued.append(ExperimentInput(parse_time(item["at"], label + ".at"),
                                      item["endpoint"], item["value"]))
    # The optional application owns the shared assertion/factory admission.
    # Import only for an explicit experiment; scene-only validation stays light.
    from sim.apps.experiment import admit_experiment
    admitted = admit_experiment(scene, registry, inputs=tuple(queued),
                                assertions=assertions, ready=ready, descriptions=descriptions)
    return Scene(topology, mode, quantum, duration,
                 ExperimentSpec(ready, admitted.inputs, tuple(assertions)))
