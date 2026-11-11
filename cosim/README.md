# DM-MC02 portable device codecs

This directory owns the C wire codecs shared by the QEMU board and its host,
and the board-independent NOR storage and persistence implementation.
The files use only the C standard library. Host code compiles the wire codecs
from its pinned QEMU checkout; QEMU does not depend on the host repository.

Imported without semantic changes from `cosim/` in platform commit
`97bf0c7f44c6fdfe6a0810ab724e8455de63e7c4` of
<https://github.com/lym12321/qemu-dm-mc02>.

- `dm_mc02_wire.*`: v1 frame codec and typed payload validation.
- `dm_mc02_v2_wire.*`, `dm_mc02_v2_payload.*`: v2 framing and payload codecs.
- `dm_nor_flash.*`, `dm_nor_flash_persistence.*`: NOR storage and file persistence.

Transport, host sessions, physics and the desktop application live in
<https://github.com/lym12321/dm-mc02-simulator>.
