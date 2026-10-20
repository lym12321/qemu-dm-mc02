"""The single public scene schema, independent of registered implementations."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from sim.api import LinkSpec, NodeSpec
from sim.runtime import Simulation, Topology

from .yaml import ManifestError, fields, identifier, load_document, parse_time, text_bytes


@dataclass(frozen=True)
class Scene:
    topology: Topology
    mode: str = "stepped"
    quantum_ns: int = 1_000_000
    duration_ns: int = 200_000_000


def load_scene(path: str | Path, registry: Mapping) -> Scene:
    """Read, describe and validate every component without constructing one."""
    source, document = load_document(path)
    fields(document, {"schema", "runtime", "nodes", "links"},
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
    Simulation(topology, registry, mode=mode, quantum_ns=quantum).validate()
    return Scene(topology, mode, quantum, duration)
