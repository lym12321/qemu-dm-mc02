"""Protocol-neutral registry for lazily constructed simulation backends."""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Callable, Mapping
from typing import Any, TypeAlias


BackendFactory: TypeAlias = Callable[[argparse.Namespace], Any]


class BackendRegistry:
    """Map stable backend names to factories.

    A registry owns only factory references.  Backend instances are created on
    demand, so separate registries can be used by tests or independent tools
    without sharing registration state.
    """

    def __init__(self, factories: Mapping[str, BackendFactory] | None = None) -> None:
        self._factories: dict[str, BackendFactory] = {}
        if factories is not None:
            for name, factory in factories.items():
                self.register(name, factory)

    def register(self, name: str, factory: BackendFactory) -> None:
        """Register *factory* under *name*, failing on invalid or duplicate names."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("backend name must be a non-empty string")
        if not callable(factory):
            raise TypeError(f"backend factory for {name!r} must be callable")
        if name in self._factories:
            raise ValueError(f"backend {name!r} is already registered")
        self._factories[name] = factory

    def get(self, name: str) -> BackendFactory:
        """Return the factory registered under *name*."""
        try:
            return self._factories[name]
        except KeyError as exc:
            raise KeyError(f"unknown backend {name!r}") from exc

    def create(self, name: str, config: argparse.Namespace) -> Any:
        """Construct a backend using the registered factory and *config*."""
        return self.get(name)(config)

    def names(self) -> tuple[str, ...]:
        """Return registered names in registration order."""
        return tuple(self._factories)

    def clear(self) -> None:
        """Remove all registrations owned by this registry."""
        self._factories.clear()


def load_callable(spec: str) -> Callable[..., Any]:
    """Load a callable from a ``module:attribute`` specification."""
    if not isinstance(spec, str) or not spec.strip() or spec.count(":") != 1:
        raise ValueError("callable spec must be in the form 'module:attribute'")

    module_name, attribute_name = spec.split(":")
    if not module_name or not attribute_name:
        raise ValueError("callable spec must be in the form 'module:attribute'")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(f"cannot import backend module {module_name!r}") from exc

    try:
        candidate = getattr(module, attribute_name)
    except AttributeError as exc:
        raise AttributeError(
            f"backend module {module_name!r} has no attribute {attribute_name!r}"
        ) from exc
    if not callable(candidate):
        raise TypeError(f"backend target {spec!r} is not callable")
    return candidate


def load_factory(spec: str) -> BackendFactory:
    """Load a backend factory from ``module:attribute``."""
    return load_callable(spec)  # type: ignore[return-value]


def load_registry(registry: BackendRegistry, spec: str) -> BackendRegistry:
    """Load and invoke a registry initializer from ``module:attribute``."""
    initializer = load_callable(spec)
    initializer(registry)
    return registry
