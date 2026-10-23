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

## Completed IMU observations

The separate IMU fixture has one hinge and a native gyro/accelerometer pair at
an offset site. Run it through the same installed CLI:

```sh
uv run sim validate examples/shared-world/imu.yaml
uv run --extra mujoco sim run examples/shared-world/imu.yaml
```

At the interactive prompt, enter:

```text
inject world.effort {"shaft":0.4}
step 10ms
inject world.effort {"shaft":0}
step 10ms
quit
```

`physics.state` includes `imus.body`, with gyro in rad/s and proper acceleration
in m/s², expressed in the site's local axes. Initially the values are `(0,0,0)`
and `(0,0,9.81)`. At 10ms of +0.4Nm, they are `(0,0,0.2)` and
`(-0.01,5,9.81)`: the offset site observes centripetal and tangential acceleration.
Native MuJoCo owns these observations and their gravity/frame semantics.

The `imus` mapping uses topology identifier aliases and literal native sensor
names in `[gyro, accelerometer]` order. Both sensors must share one site. A world
may contain only IMUs, with `joints` omitted; it then has no effort input.
Bound worlds expose the same IMU outputs while retaining their joint executor.

To receive every completed sample in another component:

```sh
uv run --extra mujoco python examples/shared-world/imu_consumer.py
```

This example registers a small custom consumer and connects
`world.imu.body → imu_wire.signal → monitor.imu`. The `net.imu` connection requires
one source and at least one sink. Sources expose `read_imu()`; sinks implement
`receive_imu(ImuSignalSample)`. Every completed cut, including the initial cut and
unchanged values, produces one broadcast. All sources are sampled before any
delivery, so a received value first affects a consumer's next interval.

`ImuSignalSample.sample` is the immutable SI `ImuSample`; `mapped_time_ns` identifies
its completed Simulation cut. Native observations have `source_time_ns=None`,
because the solver retains floating-point seconds rather than an integer sensor
timestamp. Events use `sampled` quality. `net.imu.sample` and `net.imu.delivery`
include the source and sample; deliveries also identify destination, broadcast
sequence and due time. A broadcast consumes one dispatch budget unit regardless
of fanout. Queue and dispatch limits default to 256 (allowed range 1..65536),
with at most 256 endpoints. Overflow or delivery failure freezes execution;
partial sink effects are retained and never replayed. Connections borrow their
endpoints and do not reset or close them.

The fixed `machine.qemu.imu` input accepts the same connection and opens the
existing co-sim transport only when wired. See the [firmware IMU readback
example](../imu-readback/README.md) for a complete world-to-SPI consumer. Its
zero-time observation stays on the host; each positive completed cut supplies
one sensor input for subsequent CPU execution. `imu-input-accepted` reports
protocol admission, while `imu-consumption` retains actual consumed/missing
masks. A final input can remain pending when the run ends.

These observations do not establish physical calibration, sensor ODR/filter
history, real-time performance or CPU/world lockstep.
