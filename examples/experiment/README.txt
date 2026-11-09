# Finite firmware experiment

On Linux, from the repository root:

```sh
cmake -S examples/minimal-board -B build/experiment-firmware -DCMAKE_BUILD_TYPE=Debug
cmake --build build/experiment-firmware
cargo run --release -p sim-host --features scene,machine --example experiment -- \
  --qemu build/qemu/qemu-system-arm --firmware build/experiment-firmware/firmware.elf
```

The compiled example calls the existing experiment app, presses the real board
key at 40 ms and releases it at 90 ms, then checks UART and GPIO observations.
Assertions use Simulation time zero; UART times are observation upper bounds.
The app returns its result after native resource cleanup. `--backend-root`
selects the checkout used for native resources when it differs from this one.
