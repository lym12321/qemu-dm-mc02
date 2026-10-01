/* Project-owned, board-independent synchronous W25Q64 SSI device. */
#ifndef HW_BLOCK_DM_W25Q64_H
#define HW_BLOCK_DM_W25Q64_H

#include "hw/qdev-core.h"

#define TYPE_DM_W25Q64 "dm-w25q64"
#define DM_W25Q64_SIZE (8u * 1024 * 1024)

/* Borrowed for alias/persistence only; the realized device owns the bytes. */
uint8_t *dm_w25q64_storage(DeviceState *dev);
uint32_t dm_w25q64_size(DeviceState *dev);

/* Byte SSI profile: 0b/6b have one dummy token; eb has a mode byte and
 * two dummy tokens (four quad clocks). No lane/QE/electrical timing model. */
#endif
