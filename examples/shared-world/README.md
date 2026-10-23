# One world, two joints

This fixture runs the existing MuJoCo backend through the same `Simulation` used
for firmware. It needs Python 3.11+ and the pinned `mujoco` extra; QEMU and an ARM
toolchain are unnecessary. From the repository root:

```sh
uv sync --locked --extra mujoco
uv run sim validate examples/shared-world/scene.yaml
uv run sim run examples/shared-world/scene.yaml --report build/shared-world.json
```

Enter these commands at the interactive prompt:

```text
inject world.effort {"j1":0.4,"j2":-0.4}
step 10ms
inject world.effort {"j1":0,"j2":0}
step 10ms
quit
```

`physics.state` events contain both immutable joint states and `native_time_s`.
At 10 ms, `j1` has position 0.0011 rad and speed 0.2 rad/s; `j2` has the opposite
signs. After another 10 ms at zero torque, positions are ±0.0031 rad and speeds
remain ±0.2 rad/s. The JSON report is created after cleanup; choose a new path
for each run. Events appear on stdout.

The same fixture can be driven through the public API:

```python
from sim.composition import builtin_registry
from sim.config.scene import load_scene
from sim.runtime import Simulation

registry = builtin_registry()
scene = load_scene("examples/shared-world/scene.yaml", registry)
with Simulation(scene.topology, registry, quantum_ns=scene.quantum_ns,
                on_event=lambda event: print(event)) as simulation:
    simulation.schedule_input("world.effort", {"j1": .4, "j2": -.4}, at_ns=0)
    simulation.schedule_input("world.effort", {"j1": 0, "j2": 0}, at_ns=10_000_000)
    simulation.run_until(scene.duration_ns)
```

An effort batch updates only the named joints; other held controls persist until
changed or reset. All entries are checked before any control write. A scheduled
change takes effect at its delivery cut, after integration up to that cut. One
world advances once per internal Simulation interval, irrespective of the number
of joints. `reset` closes and reconstructs the world with zero controls.

The model has two independent hinge inertias of 0.02 kg·m², unit-gear motors,
zero gravity and no damping or contacts. Its values are a mechanics fixture,
not physical calibration. Native floating-point solver time is retained in the
payload; sampled events use the integer Simulation cut and have no integer source
timestamp. The world is independent of motor protocols and firmware. For a
firmware-driven version, see [the two-motor example](../can-motor/README.md).
It selects `control: bound`, connects each motor's `joint` port to
`world.drives`, and lets one shared executor apply all motor controls before
advancing the world. Bound worlds accept no external `world.effort` inputs;
unbound joints keep their reset-zero controls. Native joint names are literal
motor `joint` options, so they need not be valid topology port names.

Direct control remains the default. Neither mode establishes CPU/world lockstep
or real time performance. Invalid models/bindings fail at execution; validation checks
configuration without loading MuJoCo. Missing native dependencies report
`blocked` (exit 78), while execution failures report `failed` (exit 1).
