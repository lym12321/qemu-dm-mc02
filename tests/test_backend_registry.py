import argparse
import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "tools" / "dm_mc02_backend_registry.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_backend_registry_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
registry_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry_module)

BackendRegistry = registry_module.BackendRegistry


def loaded_factory(config):
    return {"kind": "loaded", "config": config}


def loaded_registry(registry):
    registry.register("loaded", loaded_factory)


not_callable = 1


def test_register_get_and_create_passes_configuration_unchanged():
    registry = BackendRegistry()
    config = argparse.Namespace(rate_hz=1000)

    def factory(received):
        return received

    registry.register("null", factory)

    assert registry.names() == ("null",)
    assert registry.get("null") is factory
    assert registry.create("null", config) is config


def test_empty_name_and_duplicate_registration_fail_clearly():
    registry = BackendRegistry()
    factory = lambda config: config

    with pytest.raises(ValueError, match="non-empty"):
        registry.register(" ", factory)
    registry.register("null", factory)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("null", factory)


def test_unknown_backend_name_fails():
    with pytest.raises(KeyError, match="unknown backend"):
        BackendRegistry().get("missing")


def test_dynamic_factory_loading_from_module_attribute():
    factory = registry_module.load_factory(f"{__name__}:loaded_factory")
    config = argparse.Namespace(seed=7)

    assert factory(config) == {"kind": "loaded", "config": config}


def test_dynamic_registry_loading_invokes_initializer():
    registry = registry_module.load_registry(
        BackendRegistry(), f"{__name__}:loaded_registry"
    )

    assert registry.names() == ("loaded",)


@pytest.mark.parametrize("spec", ["", "missing", ":factory", "module:", "a:b:c"])
def test_invalid_dynamic_spec_fails(spec):
    with pytest.raises(ValueError, match="module:attribute"):
        registry_module.load_callable(spec)


def test_dynamic_target_errors_are_specific():
    with pytest.raises(ImportError, match="cannot import backend module"):
        registry_module.load_callable("module_that_does_not_exist:factory")
    with pytest.raises(AttributeError, match="has no attribute"):
        registry_module.load_callable(f"{__name__}:missing")
    with pytest.raises(TypeError, match="not callable"):
        registry_module.load_callable(f"{__name__}:not_callable")


def test_clear_is_instance_local_and_allows_fresh_registration():
    first = BackendRegistry()
    second = BackendRegistry()
    factory = lambda config: config
    first.register("null", factory)
    second.register("other", factory)

    first.clear()

    assert first.names() == ()
    assert second.names() == ("other",)
    first.register("fresh", factory)
    assert first.names() == ("fresh",)
