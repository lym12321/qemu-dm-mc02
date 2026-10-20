Build and run from the repository root:

  cmake -S examples/minimal-board -B examples/minimal-board/build -DCMAKE_BUILD_TYPE=Debug
  cmake --build examples/minimal-board/build
  sim validate examples/minimal-board/scene.yaml
  sim run examples/minimal-board/scene.yaml
  bash tools/run-simulation-smoke.sh --case minimal-board

Dependencies: Python core installation, CMake, arm-none-eabi-gcc, and the
project's dm-mc02 QEMU build plus its pinned QMP client. No trobot, HAL SDK,
Robot, physics engine or network download is required to build this fixture.

This small real bare-metal ELF uses the DM-MC02 profile at reset HSI64MHz.
USART1 emits SIM_READY minimal-board and KEY released B0=0. The acceptance
runner schedules the actual PA15 user-key input, observes PB0 through native
board-state, and requires pressed/released UART lines from guest execution.
PB0 is a digital output; this is not an electrical LED or silicon timing test.
The ordinary scene runs for 200 logical milliseconds without a key action.

Register definitions follow STM32H723 CMSIS/RM0468. The example shares only its
startup, linker and build settings with the sibling can-motor example.
