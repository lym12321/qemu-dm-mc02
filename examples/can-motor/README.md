# Two motors in one native world

The optional joint-loop variant uses one actual bare-metal DM-MC02 guest, two
C620 protocol devices and the existing shared MuJoCo world. Install the pinned
`mujoco` extra and build QEMU as described in the main usage guide. The original
fixed-rotor and rigid-rotor examples remain available in [README.txt](README.txt).

From the repository root:

```sh
cmake -S examples/can-motor -B examples/can-motor/build-joint-loop \
  -DCMAKE_BUILD_TYPE=Debug -DSIM_JOINT_LOOP_TEST=ON -DSIM_ROTOR_LOOP_TEST=OFF
cmake --build examples/can-motor/build-joint-loop
uv run --frozen --extra mujoco sim validate examples/can-motor/joint-loop.yaml
uv run --frozen --extra mujoco sim run examples/can-motor/joint-loop.yaml
SIM_PYTHON=.venv/bin/python bash tools/run-simulation-smoke.sh --case joint-loop
# Rebuild the same admitted variant path and repeat with Release:
SIM_PYTHON=.venv/bin/python bash tools/run-simulation-smoke.sh --case joint-loop --build-type Release
```

`SIM_JOINT_LOOP_TEST` defaults to OFF and is mutually exclusive with
`SIM_ROTOR_LOOP_TEST`. The scene runs for 80ms with 1ms quantum. Motor1 maps to
native `j1`, motor2 to `j2`; each motor keeps its own C620 state and feedback ID.
The guest sends one `0x200` batch containing raw +4096/-4096 (ideal +5A/-5A),
wire `10 00 f0 00 00 00 00 00`. Both actual feedback reads must show the
corresponding signed current, signed RPM magnitude at least2 and nonzero encoder
before the guest submits its only all-zero command. Submission precedes
`JOINT_MOVING ids=1,2`. Actual zero-current feedback with the corresponding motion
sign must then be read for both IDs before `JOINT_COAST ids=1,2` and `JOINT_PASS`.
Missing completion within 75ms of the drive command or malformed feedback emits
`JOINT_FAIL`.

The fixture reuses the two independent native hinges with inertia0.02kg·m²,
zero gravity, no damping or contacts, and the ideal M3508 P19 ratio3591/187.
Torque/current and gearbox values are uncalibrated approximations. Acceptance
uses the actual drive/zero Fabric delivery cuts and an independent closed form
of native Euler motion to check both joint states and signed feedback bytes.
Actual deliveries, native transport receipts and later guest commands/UART are
retained separately. The guest branches establish the two feedback reads;
receipts alone establish transport processing. Integer observation cuts do not
replace native floating-point solver time or establish CPU/world lockstep.

`--case joint-loop` is an explicit optional MuJoCo gate. `--case all` retains
the original five cases and their dependencies. The shared-world gate uses the
same bounded lifecycle, preserves failed observations, and publishes success
only after native resource cleanup and the firmware ELF hash check. It makes no
physical calibration or real-time performance claim.
