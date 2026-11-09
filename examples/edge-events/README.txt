# Explicit digital edges

From the repository root:

```sh
cargo run --release -p sim-host --features simulation --example edge_events
```

The compiled source and receiver use the public Simulation and digital-edge
connection APIs. Four scripted edges, including two at 101 ns, survive one
1 ms interval. Output keeps source time, mapped time and delivery cut separate.
The existing `edge` test covers the exact script. These events do not represent
an observed QEMU GPIO or UART waveform.
