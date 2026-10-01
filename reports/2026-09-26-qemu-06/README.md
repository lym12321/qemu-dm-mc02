# QEMU-06 source delivery evidence

Date: 2026-09-26 UTC. Owner: source-delivery tooling. Producer: canonical
project source and nested QEMU fork; boundary: local tar package and JSON
manifest; direct consumer: restore into a new empty directory. This report
records the QEMU-06 stage; subsequent independent build evidence is in
[QEMU-07](../2026-09-26-qemu-07/README.md).

## Source and external inputs

- Outer HEAD before this slice: `254b23ee4a850284162b6ca27d1f8c003b346ebd`.
  The package also includes current outer tracked edits and necessary untracked
  files. Nested QEMU fork: `73ac4b85cfcd16a31211e2158fcd9788b7d8c429`,
  based on upstream v8.2.2 commit `11aa0b1ff115b86160c4d37e7c37e6a6b13b77ea`.
  The fork contains 258 changed paths relative to that upstream base.
- Included wrap Git revisions: `dtc` `b6910bec11614980a21e46fbccc35934b671bd81`,
  `keycodemapdb` `f5772a62ec52591ff6870b7e8ef32482371f22c6`,
  `berkeley-softfloat-3` `b64af41c3276f97f0e181920400ee056b9c88037`,
  `berkeley-testfloat-3` `e7af9751d9f9fd3b47911f51a5cfd08af256a9ab`.
- External firmware input, not bundled:
  `../trobot/build/Release-current/trobot.elf`, 643384 bytes,
  SHA-256 `40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`.
- QEMU and Host binary identities stayed unchanged across the test gate; QEMU
  SHA-256 was `29175e5f691cd73864f88b1765f7e1859281af4db32c95952e28d1f1d60dc248`.

## Checks

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_source_package.py
python3 tools/dm_mc02_source_package.py create build/source-packages/qemu06-delivery-20260926.tar.gz
python3 tools/dm_mc02_source_package.py verify build/source-packages/qemu06-delivery-20260926.tar.gz
python3 tools/dm_mc02_source_package.py restore PACKAGE NEW_EMPTY_DIRECTORY
python3 tools/dm_mc02_test_gate.py --no-build --jobs 4
```

The four isolated tests passed (valid restore/existing destination, changed
source bytes, path traversal, and required wrap overlays). The corrected local
package includes 11,608 source paths;
verify and a temporary empty-directory restore rechecked every file and mode.
The canonical gate exited 0: Meson 65/65, native Host CTest 51/51, pytest
299/299, shell smoke 94/94. ROS2/MuJoCo passed; Renode was not invoked.
The original 299-case pytest gate log is
`build/test-results/qemu-gate/20260926T122453.350960Z-367351/`.

QEMU-07 exposed a missing input after the initial successful byte-only restore:
the Berkeley wrap Git repositories do not themselves contain `meson.build`;
QEMU's tracked `subprojects/packagefiles` supplies two files for each via
`patch_directory`. The source packager now puts those four files into the wrap
build trees as well as retaining their original QEMU paths. The first package
was replaced and must not be used as rebuild evidence.

## Limits and next gate

Package location: `build/source-packages/qemu06-delivery-20260926.tar.gz`.
SHA-256: `5f6c1cb9c9866a92f45fded6ce3a966c803b5ddaa653801027a854b95232c06f`.
The tar file is a local ignored artifact; it is not a Git commit or publication.
It excludes generated build trees, ROM submodule contents, Python wheels,
installed system packages and external firmware bytes. The manifest records
observed build tools and dependencies, the build profile, upstream/fork
identities and content hashes. At the QEMU-06 stage, no independent compile
had run inside the restored tree. QEMU-07 subsequently configured and built
that restored source package, then passed the canonical gate with the same
firmware identity; see its report for exact evidence and limits.

Changed files: `tools/dm_mc02_source_package.py`,
`tests/test_source_package.py`, `AGENTS.md`, `ARCHITECTURE.md`,
`INTERFACES.md`, `CAPABILITIES.md`, `README.md`, `PLAN.md`, `REVIEW.md`,
this report, and workspace `PLAN.md`/`PROGRESS_REPORT.md`. `trobot/` and
production model code were not changed.
