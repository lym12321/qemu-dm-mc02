# DM-MC02 QEMU engine

This repository contains the DM-MC02 board and STM32H723 device models on QEMU
8.2.2. It builds independently. The desktop application, Rust host, scenarios,
diagnostics and product packaging live in
[dm-mc02-simulator](https://github.com/lym12321/dm-mc02-simulator).

The engine owns board registers, interrupts, device timing and migration state,
QEMU-side co-simulation, and the portable C codecs in [`cosim/`](cosim/README.md).
The Simulator pins an engine commit and consumes those codecs from its checkout.
Engine development follows `main`; version checkpoints use tags and the
Simulator's `qemu.lock` records the exact engine revision.

## Build on Windows

Enable Windows Developer Mode before cloning so Git can create QEMU's symbolic
links. Install MSYS2 and use its **MINGW64** packages:

```bash
pacman -S --needed git make mingw-w64-x86_64-gcc \
  mingw-w64-x86_64-python mingw-w64-x86_64-python-pip \
  mingw-w64-x86_64-python-distlib mingw-w64-x86_64-ninja \
  mingw-w64-x86_64-pkgconf mingw-w64-x86_64-glib2 mingw-w64-x86_64-zlib
```

From PowerShell:

```powershell
git clone -c core.symlinks=true https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
.\scripts\build-dm-mc02.ps1 -MsysRoot C:\msys64
```

The executable is `build\qemu-system-arm.exe`. Pass `-BuildDir <directory>` to
place output elsewhere, `-Jobs 4` to select concurrency, and `-Reconfigure` after
changing build options. The script installs QEMU's bundled Meson wheel into a
build-local Python environment and uses the installed MINGW64 libraries.
Native Windows Meson defaults are persisted with a Windows absolute prefix so
ordinary Ninja regeneration works after source changes.

Keep `mingw64\bin` on `PATH` when running this development executable. The
Simulator's product packager supplies the required DLLs for end users.

## Build on Linux

Install a C/C++ compiler, Ninja, pkg-config, Python with venv/pip, GLib and zlib
development packages. Run:

```bash
./scripts/build-dm-mc02.sh
./build/qemu-system-arm -machine help
```

The initial configuration downloads the pinned `dtc` and `keycodemapdb` Meson
subprojects. `QEMU_BUILD_TYPE` and `QEMU_BUILD_JOBS` select build type and jobs.
For other targets and QEMU options, use the standard `configure` entry point
described in [`README.rst`](README.rst).

## Focused engine tests

Run in the same MINGW64 or Linux environment used for the build:

```bash
ninja -C build -j4 tests/unit/test-dm-cosim-link tests/unit/test-dm-w25q64 \
  tests/unit/test-dm-ospi-vmstate tests/unit/test-dm-dma-mmio \
  tests/unit/test-dm-dma-subsystem-vmstate tests/unit/test-io-channel-socket
build/pyvenv/bin/meson test -C build --no-rebuild --print-errorlogs \
  test-dm-cosim-link test-dm-w25q64 test-dm-ospi-vmstate test-dm-dma-mmio \
  test-dm-dma-subsystem-vmstate test-io-channel-socket
```

On Windows append `.exe` to Ninja executable targets.

## Board input and output

The read-only `/machine` QOM property `board-state` reports GPIO, USER/NRST,
latched RGB and timestamped buzzer changes. `user-key` and `reset-key` accept
booleans; holding `reset-key=true` keeps the CPU in reset until release.

WS2812 DIN consumes the resolved PA7 pin level at QEMU virtual nanoseconds.
CPU/DMA GPIO writes, TIM1_CH1N/AF1, TIM3_CH2/AF2, TIM8_CH1N/AF3,
SPI1_MOSI/AF5 and SPI6_MOSI/AF8 feed the same decoder. Timer outputs retain
polarity, preload and dead-time behavior. SPI1 uses RCC SPI123SEL, its 16-byte TX FIFO and DMAMUX request 38
on either DMA controller. TX supports 4–32-bit frames, packed writes, bit
order, CPHA, MBR, MSSI/MIDI and EOT IRQ35. SPI6 has an 8-byte FIFO,
4–16-bit frames, IRQ86 and RCC SPI6SEL / APB4 gate and reset; its BDMA path
is not connected. SPI2 keeps its existing synchronous
BMI088 path. Timed SPI RX retains one result; full RX FIFO and timed SPI
component migration require separate implementation. Changing an active SPI
clock first delivers due edges at the old clock, then resumes the pending bit
within the constant run after one period of the new clock.

SPI schedules level changes and frame completions, coalescing adjacent equal
bits while retaining FIFO/DMA/EOT frame boundaries. MMIO accesses first deliver
due events, so polling sees current FIFO state and late writes cannot fill past
gaps. GPIO mode/AF routing is cached; pad changes still synchronize old producer
edges before resolving the new electrical state. Unrelated GPIO banks do not
recheck the LED or buzzer routes. The LED reuses one pending reset check across
pulses. Timer/DMA events retain scheduled timestamps through GPIO writes.

`led.source=ws2812-din` includes `timing_profile`, `frames`, `errors`,
`latched_ns` and `powered`. The `worldsemi-ws2812b-classic` reference profile
uses the [Worldsemi WS2812B sheet](https://cdn-shop.adafruit.com/datasheets/WS2812B.pdf):
T0H/T0L 400/850 ns, T1H/T1L 800/450 ns, each ±150 ns; reset low ≥50 µs;
GRB, MSB first. The schematic's WS2812B-3528 revision still needs matching to
its purchasing-part datasheet. Bit values depend on high pulse width; low
intervals below 50 µs do not reject a bit. Invalid frames retain the last latched
color. DIN is an ideal digital node: a released pad retains its last resolved
level until another driver or pull changes it. Initially unknown DIN stays
unknown, and power changes clear the node and decoder. This approximation omits
leakage and noise; it is not a hardware guarantee of floating input retention.

High-rate timer workloads can opt into bounded event-window scheduling with
`DM_TIM_EVENT_WINDOW_NS=5000`. The timer still replays each output and DMA
timestamp in order, while the host timer queue wakes at 5 us boundaries and
MMIO accesses flush pending events before they observe or change state. This
mode is intended for sustained real-time runs such as WS2812 animation; leave
the variable unset when strict one-event-at-a-time scheduling or migration is
required. The board rejects the window for DMA endpoints and GPIO banks whose
side effects need exact global interleaving.

PB15/TIM12_CH2 publishes frequency/duty changes with generation and sequence
in a 256-event ring. GPIO rise/fall/rise periods also produce tones; two missing
periods stop the inferred tone at the last cycle's end. GPIO DC levels remain
observable through the existing raw buzzer QOM properties.

Focused checks for these boundaries:

```bash
ninja -C build tests/unit/test-dm-gpio-output tests/unit/test-dm-tim-output \
  tests/unit/test-dm-spi-output tests/unit/test-dm-ws2812 tests/unit/test-dm-buzzer \
  tests/unit/test-dm-reset-input tests/qtest/dm-mc02-reset-test tests/qtest/dm-mc02-tim2-test
build/pyvenv/bin/meson test -C build --no-rebuild --print-errorlogs \
  test-dm-gpio-output test-dm-tim-output test-dm-spi-output test-dm-ws2812 \
  test-dm-buzzer test-dm-reset-input
QTEST_QEMU_BINARY=build/qemu-system-arm build/tests/qtest/dm-mc02-reset-test
QTEST_QEMU_BINARY=build/qemu-system-arm build/tests/qtest/dm-mc02-tim2-test
```
