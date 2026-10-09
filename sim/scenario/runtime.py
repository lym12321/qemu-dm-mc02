"""Execute admitted Scenario inputs through the existing Arena owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sim.arena.manifest import ArenaManifest
from sim.manifest import MAX_TIME_NS
from sim.scenario.manifest import ScenarioManifest, ScenarioReferee

if TYPE_CHECKING:
    from sim.arena.runtime import ArenaResult


def run_manifest(manifest: ScenarioManifest, *, emit=None, on_response=None,
                 qemu=None, startup_timeout=10.0, timeout=30.0,
                 referee_period_ns=100_000_000) -> ArenaResult:
    """Delegate once; Arena owns event admission, execution and cleanup.

    Events use the shared reset origin. Duration is active world time after all
    requested ready markers, exactly as in the referenced Arena.
    """
    if type(manifest) is not ScenarioManifest:
        raise TypeError("ScenarioManifest is required")
    if type(manifest.arena) is not ArenaManifest:
        raise TypeError("Scenario requires an ArenaManifest")
    if type(manifest.duration_ns) is not int:
        raise TypeError("Scenario duration_ns must be a plain integer")
    if not 1 <= manifest.duration_ns <= MAX_TIME_NS:
        raise ValueError("Scenario duration_ns must be in 1ns..3600s")
    if (type(manifest.referees) is not tuple
            or any(type(item) is not ScenarioReferee for item in manifest.referees)):
        raise TypeError("Scenario referees require a plain ScenarioReferee tuple")
    if any(type(item.member) is not str or type(item.events) is not tuple
           for item in manifest.referees):
        raise TypeError("Scenario referees require plain member strings and event tuples")
    batches = tuple((item.member, item.events) for item in manifest.referees)

    from sim.arena.runtime import run_manifest as run_arena

    # The bounded binary64 round trip has <0.000472ns error; Arena's existing
    # round(seconds * 1e9) recovers every admitted integer nanosecond duration.
    return run_arena(manifest.arena, duration=manifest.duration_ns / 1_000_000_000,
        referee_events=batches, emit=emit, on_response=on_response, qemu=qemu,
        startup_timeout=startup_timeout, timeout=timeout,
        referee_period_ns=referee_period_ns)
