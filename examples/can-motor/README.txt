Build and run from the repository root:

  cmake -S examples/can-motor -B examples/can-motor/build -DCMAKE_BUILD_TYPE=Debug
  cmake --build examples/can-motor/build
  sim validate examples/can-motor/scene.yaml
  sim run examples/can-motor/scene.yaml
  bash tools/run-simulation-smoke.sh --case can-motor

Dependencies: Python core installation, CMake, arm-none-eabi-gcc, and the
project's dm-mc02 QEMU build plus its pinned QMP client. This is one virtual
M3508/C620 on one CAN bus. No Robot, four-wheel XML, IMU, trobot or physics
engine is required. The explicit fixed-rotor plant has position and speed zero.

The actual guest powers PC15, configures FDCAN1 classic eight-byte FIFO/filter
elements, and sends one 0x200 current command: 00 40 00 00 00 00 00 00.
It receives 0x201 feedback and checks encoder=0, rpm=0, temperature=25,
reserved=0 and current=64. With no further command, current expires after
100ms. A real 1ms SysTick handler, not an instruction-count delay, checks this
phase. USART1 reports SIM_READY can-motor, MOTOR_ACTIVE, MOTOR_TIMEOUT and
MOTOR_PASS. Host acceptance also checks actual CAN event sources/times and
successful Simulation cleanup. A guest failure reports MOTOR_FAIL.

This validates a digital firmware/protocol boundary. Fixed rotor/current are
explicit model inputs and commanded values, not calibrated mechanics or an
electrical current measurement. Machine execution cuts, host CAN delivery,
FIFO receipt and guest consumption remain distinct events.

Feedback-driven rotor variant (the default build above is unchanged):

  cmake -S examples/can-motor -B examples/can-motor/build-rotor-loop -DCMAKE_BUILD_TYPE=Debug -DSIM_ROTOR_LOOP_TEST=ON
  cmake --build examples/can-motor/build-rotor-loop
  sim validate examples/can-motor/rotor-loop.yaml
  sim run examples/can-motor/rotor-loop.yaml
  bash tools/run-simulation-smoke.sh --case rotor-loop

SIM_ROTOR_LOOP_TEST defaults to OFF. The variant uses the same board startup,
FDCAN1 setup and SIM_READY can-motor marker. It sends exactly one raw4096 (5A)
command, wire 10 00 00 00 00 00 00 00. Only after reading valid feedback with
current4096, rpm>=2 and a nonzero encoder does the guest send one zero-current
command. It sends zero before logging ROTOR_MOVING rpm>=2, then waits for actual
feedback with current0, positive rpm and a nonzero encoder before reporting
ROTOR_COAST current=0 motion=1 and ROTOR_PASS. Missing completion within 75ms
of the initial command, or malformed feedback, reports ROTOR_FAIL. The 80ms
scene with 1ms quantum ends before the model's 100ms command timeout.

This variant reuses the existing uncalibrated rigid rotor: inertia 0.02kg m^2,
damping 0.08N m s/rad and torque constant 0.02N m/A, initially at rest. It
demonstrates feedback causing a subsequent firmware command and moving coast
state; it does not establish electrical measurements, real motor mechanics or
a global CPU/plant time barrier.
