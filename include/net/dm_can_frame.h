/* Protocol-neutral frame used by controller adapters and test fixtures. */
#ifndef NET_DM_CAN_FRAME_H
#define NET_DM_CAN_FRAME_H

#include <stdint.h>

#define CAN_FLAG_EXTENDED (1u << 0)
#define CAN_FLAG_RTR      (1u << 1)
#define CAN_FLAG_FD       (1u << 2)
#define CAN_FLAG_BRS      (1u << 3)
#define CAN_FLAG_MASK     (CAN_FLAG_EXTENDED | CAN_FLAG_RTR | \
                           CAN_FLAG_FD | CAN_FLAG_BRS)

typedef struct DmCanFrame {
    uint32_t can_id;
    uint32_t flags;
    uint8_t dlc;
    uint8_t tx_index;
    uint8_t data[64];
    uint64_t timestamp_ns;
    /* Optional scheduling metadata used only by the legacy test fixture. */
    uint64_t duration_ns;
} DmCanFrame;

/* Temporary source-compatibility name for the test-only local medium. */
typedef DmCanFrame DmMc02CanFrame;

#endif
