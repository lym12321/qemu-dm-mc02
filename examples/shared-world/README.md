# One native world, compiled consumers

Build against MuJoCo 3.12.0 as described in the main usage guide. From the
repository root:

```sh
cargo run --release -p sim-host --features simulation,mujoco,devices --example mixed_drivers
cargo run --release -p sim-host --features scene,mujoco --example imu_consumer
```

`mixed_drivers` binds a C620/M3508 driver and an ideal torque fixture to the
existing two-hinge `world.xml`. The public `JointExecution` owns the single
native world; each driver only prepares and completes its own joint plan.
One real CAN command applies raw +4096 to motor 1 with ideal gear ratio 2 and
a 3 ms timeout. The other driver applies -0.4 Nm for 7 ms. One 10 ms step splits
at both expiries, reads completed joint states and emits C620 feedback. The
ideal driver represents a scripted input; it has no motor or electrical model.

`imu_consumer` loads `imu.yaml`, registers a compiled monitor and borrows
completed site-local SI samples through `net.imu`. It applies +0.4 Nm for 10 ms
and then coasts through 20 ms. Samples retain their mapped cut; gyro is rad/s
and proper acceleration is m/s². The native world computes both sensor vectors.

The fixtures are uncalibrated. They establish shared execution and public
consumer boundaries, without a firmware/CPU time barrier or physical calibration.
