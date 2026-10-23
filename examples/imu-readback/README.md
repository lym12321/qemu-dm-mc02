# IMU firmware readback

This example connects the completed native MuJoCo site observation to the
DM-MC02 BMI088 model through `net.imu` and the existing co-simulation transport.
The real bare-metal guest configures both dies at 100 Hz, +/-1000 degrees/s and
+/-6 g, reads their six-byte SPI bursts, then stores and prints sensor time.

```sh
cmake -S examples/imu-readback -B examples/imu-readback/build -DCMAKE_BUILD_TYPE=Debug
cmake --build examples/imu-readback/build
sim validate examples/imu-readback/scene.yaml
sim run examples/imu-readback/scene.yaml
python smoke/simulation_smoke.py --case imu-readback --build-type Debug --qemu /path/to/qemu-system-arm
```

Install the project's MuJoCo extra to run the native world. The acceptance gate
requires two increasing UART readbacks matched to actual complete gyro and
accelerometer consumption receipts. At rest, the native SI acceleration is
`(0, 0, 9.81)` m/s²; the expected Z raw code is `5463`, which distinguishes this
input from the device's default 1 g value. Startup missing samples and final
pending inputs remain visible in the report.

`SIM_IMU_SKIP_GYRO_TEST=ON` builds a test-only mutant. It prints the same zero
gyro bytes without performing the burst, and the unchanged acceptance rejects
its missing gyro consumption. This option is off by default.

The model is an uncalibrated mechanics fixture. Input time, native transport
receipts and guest consumption have distinct meanings; this example establishes
neither global CPU/world lockstep nor physical ODR/filter/timing accuracy.
