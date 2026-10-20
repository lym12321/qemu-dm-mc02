"""Immutable topology descriptions; validation performs no resource creation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}\Z")


def _name(value: str, what: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"{what} must be an identifier of at most 128 characters")


def _port_name(value: str) -> None:
    if (not isinstance(value, str) or len(value) > 128
            or not all(_NAME.fullmatch(part) for part in value.split("."))):
        raise ValueError("port name must contain identifiers separated by dots")


def immutable(value: Any) -> Any:
    """Snapshot public data without retaining mutable containers or byte views."""
    if value is None or type(value) in (bool, int, float, str, bytes):
        return value
    if isinstance(value, Path):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("public data mapping keys must be strings")
        return MappingProxyType({key: immutable(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(immutable(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(immutable(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        # Frozen records such as CanFrame may be kept only if every field is
        # already immutable. Rejecting mutable records avoids a false snapshot.
        if not value.__dataclass_params__.frozen:
            raise TypeError("public data records must be frozen dataclasses")
        for item in fields(value):
            original = getattr(value, item.name)
            if not _record_field_is_immutable(original):
                raise TypeError("frozen public records must contain immutable fields")
        return value
    raise TypeError(f"unsupported public data value: {type(value).__name__}")


def _record_field_is_immutable(value: Any) -> bool:
    if value is None or type(value) in (bool, int, float, str, bytes) or isinstance(value, Path):
        return True
    if isinstance(value, (tuple, frozenset)):
        return all(_record_field_is_immutable(item) for item in value)
    if isinstance(value, MappingProxyType):
        return all(isinstance(key, str) and _record_field_is_immutable(item) for key, item in value.items())
    if is_dataclass(value) and not isinstance(value, type) and value.__dataclass_params__.frozen:
        return all(_record_field_is_immutable(getattr(value, item.name)) for item in fields(value))
    return False


def _capabilities(values: Any) -> frozenset[str]:
    if isinstance(values, str):
        raise ValueError("capabilities must be a collection of names")
    result = frozenset(values)
    if any(not isinstance(item, str) or not item or "\x00" in item for item in result):
        raise ValueError("capabilities must be nonempty strings")
    return result


@dataclass(frozen=True)
class Endpoint:
    node: str
    port: str

    def __post_init__(self) -> None:
        _name(self.node, "node name")
        _port_name(self.port)

    @classmethod
    def parse(cls, value: Endpoint | str) -> Endpoint:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or "." not in value:
            raise ValueError("endpoint must be node.port")
        return cls(*value.split(".", 1))

    def __str__(self) -> str:
        return f"{self.node}.{self.port}"


@dataclass(frozen=True)
class PortSpec:
    name: str
    protocol: str
    direction: str = "inout"
    category: str = "protocol"
    max_connections: int | None = 1
    capabilities: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset()
    min_connections: int = 0

    def __post_init__(self) -> None:
        _port_name(self.name)
        if not isinstance(self.protocol, str) or not self.protocol or "\x00" in self.protocol:
            raise ValueError("port protocol must be a nonempty string")
        if self.direction not in ("in", "out", "inout"):
            raise ValueError("port direction must be in, out or inout")
        if self.category not in ("protocol", "signal", "binding"):
            raise ValueError("port category must be protocol, signal or binding")
        if (self.max_connections is not None
                and (type(self.max_connections) is not int or self.max_connections <= 0)):
            raise ValueError("max_connections must be positive or None")
        if (type(self.min_connections) is not int or self.min_connections < 0
                or (self.max_connections is not None and self.min_connections > self.max_connections)):
            raise ValueError("min_connections must be nonnegative and not exceed max_connections")
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        object.__setattr__(self, "required_capabilities", _capabilities(self.required_capabilities))


@dataclass(frozen=True)
class NodeSpec:
    name: str
    kind: str
    options: Mapping[str, Any] = field(default_factory=dict)
    base_dir: Path = Path(".")
    required_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _name(self.name, "node name")
        if (not isinstance(self.kind, str) or not self.kind
                or len(self.kind) > 128 or "\x00" in self.kind):
            raise ValueError("node kind must be a nonempty string of at most 128 characters")
        if not isinstance(self.options, Mapping):
            raise ValueError("node options must be a mapping")
        object.__setattr__(self, "options", immutable(self.options))
        object.__setattr__(self, "base_dir", Path(self.base_dir).resolve())
        object.__setattr__(self, "required_capabilities", _capabilities(self.required_capabilities))


@dataclass(frozen=True)
class NodeDescription:
    ports: tuple[PortSpec, ...] = ()
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ports", tuple(self.ports))
        if any(not isinstance(port, PortSpec) for port in self.ports):
            raise TypeError("node ports must be PortSpec values")
        if len({port.name for port in self.ports}) != len(self.ports):
            raise ValueError("duplicate port name")
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))

    def port(self, name: str) -> PortSpec:
        for port in self.ports:
            if port.name == name:
                return port
        raise ValueError(f"unknown port {name!r}")


@dataclass(frozen=True)
class LinkSpec:
    source: Endpoint | str
    target: Endpoint | str
    required_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Endpoint.parse(self.source))
        object.__setattr__(self, "target", Endpoint.parse(self.target))
        object.__setattr__(self, "required_capabilities", _capabilities(self.required_capabilities))


@dataclass(frozen=True)
class Topology:
    nodes: tuple[NodeSpec, ...]
    links: tuple[LinkSpec, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "links", tuple(self.links))
        if not 0 < len(self.nodes) <= 64:
            raise ValueError("topology requires 1..64 nodes")
        if len(self.links) > 256:
            raise ValueError("topology permits at most 256 links")
        if any(not isinstance(node, NodeSpec) for node in self.nodes):
            raise TypeError("topology nodes must be NodeSpec values")
        if any(not isinstance(link, LinkSpec) for link in self.links):
            raise TypeError("topology links must be LinkSpec values")
        if len({node.name for node in self.nodes}) != len(self.nodes):
            raise ValueError("duplicate node name")

    def validate(self, registry: Mapping[str, Any]) -> Mapping[str, NodeDescription]:
        """Call pure factory descriptions, then validate every edge before IO.

        Edges describe connections, so their written order does not choose flow.
        An input/output pair or an inout port is required. Required capabilities
        are explicit admission requirements, not runtime feature guessing.
        Optional factory validate_topology(topology, descriptions, registry)
        hooks admit shared component resources, without adding domain policy
        here. Like describe(), those hooks must not create resources.
        """
        registry = MappingProxyType(dict(registry))
        descriptions: dict[str, NodeDescription] = {}
        for node in self.nodes:
            factory = registry.get(node.kind)
            if factory is None:
                raise ValueError(f"unknown component kind {node.kind!r}")
            if any(not callable(getattr(factory, name, None)) for name in ("describe", "create")):
                raise TypeError(f"factory {node.kind!r} requires describe and create")
            description = factory.describe(node)
            if not isinstance(description, NodeDescription):
                raise TypeError(f"factory {node.kind!r} did not return a NodeDescription")
            if not node.required_capabilities <= description.capabilities:
                raise ValueError(f"node {node.name} lacks required capabilities")
            descriptions[node.name] = description
        seen: set[frozenset[Endpoint]] = set()
        counts: dict[Endpoint, int] = {}
        for link in self.links:
            first, second = link.source, link.target
            if first.node not in descriptions or second.node not in descriptions:
                raise ValueError(f"link refers to an unknown node: {first}, {second}")
            if first == second:
                raise ValueError("a port cannot connect to itself")
            identity = frozenset((first, second))
            if identity in seen:
                raise ValueError(f"duplicate connection: {first}, {second}")
            seen.add(identity)
            try:
                a = descriptions[first.node].port(first.port)
                b = descriptions[second.node].port(second.port)
            except ValueError as exc:
                raise ValueError(f"invalid link {first}, {second}: {exc}") from exc
            if a.category != b.category or a.protocol != b.protocol:
                raise ValueError(f"connection type/protocol mismatch: {first}, {second}")
            if a.direction == b.direction and a.direction != "inout":
                raise ValueError(f"connection direction mismatch: {first}, {second}")
            if (not a.required_capabilities <= b.capabilities
                    or not b.required_capabilities <= a.capabilities
                    or not link.required_capabilities <= (a.capabilities & b.capabilities)):
                raise ValueError(f"connection lacks required capabilities: {first}, {second}")
            for endpoint, port in ((first, a), (second, b)):
                counts[endpoint] = counts.get(endpoint, 0) + 1
                if port.max_connections is not None and counts[endpoint] > port.max_connections:
                    raise ValueError(f"port connection capacity exceeded: {endpoint}")
        for node, description in descriptions.items():
            for port in description.ports:
                if counts.get(Endpoint(node, port.name), 0) < port.min_connections:
                    raise ValueError(f"port requires at least {port.min_connections} connections: {node}.{port.name}")
        admitted = MappingProxyType(descriptions)
        checked: set[int] = set()
        for node in self.nodes:
            factory = registry[node.kind]
            if id(factory) in checked:
                continue
            checked.add(id(factory))
            validator = getattr(factory, "validate_topology", None)
            if validator is not None:
                if not callable(validator):
                    raise TypeError(f"factory {node.kind!r} validate_topology must be callable")
                validator(self, admitted, registry)
        return admitted
