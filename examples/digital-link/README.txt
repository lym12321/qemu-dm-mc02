Two real firmware machines connected by a sampled digital wire

Run these commands from the repository root:

    cmake -S examples/minimal-board -B examples/minimal-board/build -DCMAKE_BUILD_TYPE=Debug -DSIM_KEY_PULSE_TEST=OFF
    cmake --build examples/minimal-board/build
    sim validate examples/digital-link/scene.yaml
    tools/run-simulation-smoke.sh --case digital-link --qemu /path/to/qemu-system-arm

Both machines execute the existing normal minimal-board ELF. Its firmware reads
PA15 and drives PB0 high while its key is pressed. The scene connects main PB0
to peer's physical key input through net.digital. A low wire level presses the
peer key; a high level releases it. External inject main.key true still means
press, so main and peer PB0 outputs show opposite phases.

The smoke presses main at 40ms and releases it at 90ms. It checks each completed
source GPIO sample, net delivery, a later actual peer GPIO response, and the
peer's ordered UART messages. It reports success after both QEMU processes and
their sockets are cleaned up, and verifies the ELF did not change during run.
The same scene can be run with sim run; the scheduled 40/90ms stimuli belong to
the smoke fixture. Python callers can use Simulation.schedule_input for them.

The wire samples completed outputs once per simulation cut. Initial undriven
GPIO leaves the peer input unchanged; losing output drive after activation
fails. Propagation cannot change a CPU interval that has already completed.
UART timestamps are observation upper bounds. This example does not model
analog voltages, electrical contention, open-drain behavior or board timing,
and establishes no global CPU or firmware-consumption barrier.
