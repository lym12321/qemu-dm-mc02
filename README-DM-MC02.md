# DM-MC02 QEMU engine

This repository contains the DM-MC02 board and STM32H723 device models on QEMU
8.2.2. It builds independently. The desktop application, Rust host, scenarios,
diagnostics and product packaging live in
[dm-mc02-simulator](https://github.com/lym12321/dm-mc02-simulator).

The engine owns board registers, interrupts, device timing and migration state,
QEMU-side co-simulation, and the portable C codecs in [`cosim/`](cosim/README.md).
The Simulator pins an engine commit and consumes those codecs from its checkout.

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
git clone -c core.symlinks=true -b dm-mc02/v8.2.2 https://github.com/lym12321/qemu-dm-mc02.git
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
build/pyvenv/bin/meson test -C build --print-errorlogs \
  test-dm-cosim-link test-dm-w25q64 test-dm-ospi-vmstate test-dm-dma-mmio \
  test-dm-dma-subsystem-vmstate test-io-channel-socket
```

On Windows append `.exe` to Ninja executable targets.
