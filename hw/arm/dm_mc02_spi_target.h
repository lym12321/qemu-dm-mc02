/* Board-independent byte and chip-select contract for an SPI target. */
#ifndef HW_ARM_DM_MC02_SPI_TARGET_H
#define HW_ARM_DM_MC02_SPI_TARGET_H

#include <stdbool.h>
#include <stdint.h>

typedef uint8_t (*DmMc02SpiTargetTransferFn)(void *opaque, uint8_t tx,
                                             uint64_t timestamp_ns);
/* Called whenever the bus chip-select mask changes.  selected is this
 * target's level and selected_mask preserves multi-select visibility for
 * adapters that need to model contention or an invalid bus combination. */
typedef void (*DmMc02SpiTargetSelectFn)(void *opaque, bool selected,
                                        uint32_t selected_mask);

typedef struct DmMc02SpiTarget {
    DmMc02SpiTargetTransferFn transfer;
    DmMc02SpiTargetSelectFn select;
    void *opaque;
} DmMc02SpiTarget;

#endif
