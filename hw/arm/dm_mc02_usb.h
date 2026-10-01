/* Minimal STM32H723 USB OTG HS (DWC2) register model for DM-MC02. */
#ifndef HW_ARM_DM_MC02_USB_H
#define HW_ARM_DM_MC02_USB_H

#include "exec/memory.h"
#include "chardev/char-fe.h"
#include "hw/usb/dm_usb_control.h"
#include "hw/usb/dm_usb_dwc2_device.h"
#include "hw/irq.h"
#include "qapi/error.h"
#include "qemu/timer.h"

#include <stdint.h>

#define DM_MC02_USB_REGION_SIZE 0x20000

/* The packet window is deliberately outside the STM32/DWC2 register map.
 * It is an in-process protocol-acceptance hook for qtests and host-side
 * tools, not a USB bus/PHY transport. FIFO0 remains the legacy raw byte pipe
 * used by existing board tests. */
#define DM_MC02_USB_TEST_RX_LEN     0x1f000
#define DM_MC02_USB_TEST_RX_DATA    0x1f004
#define DM_MC02_USB_TEST_RX_SUBMIT  0x1f008
#define DM_MC02_USB_TEST_TX_LEN     0x1f010
#define DM_MC02_USB_TEST_TX_DATA    0x1f014
#define DM_MC02_USB_TEST_STATUS     0x1f018
#define DM_MC02_USB_TEST_EP1_RX_COUNT 0x1f01c

#define DM_MC02_USB_TEST_DIR_IN     (1u << 8)
#define DM_MC02_USB_TEST_SETUP      (1u << 9)
#define DM_MC02_USB_TEST_EP_MASK    0x0fu
#define DM_MC02_USB_TEST_EP0_STALLED (1u << 19)
#define DM_MC02_USB_EP_COUNT        6
#define DM_MC02_USB_EP_PACKET_SIZE   64

typedef struct DmMc02Usb {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_USB_REGION_SIZE];
    CharBackend chr;
    uint8_t rx_fifo[4096];
    uint8_t tx_fifo[4096];
    size_t rx_head;
    size_t rx_count;
    size_t tx_head;
    size_t tx_count;
    QEMUTimer *tx_timer;
    bool enabled;
    bool opened;
    uint64_t rx_bytes;
    uint64_t tx_bytes;
    uint64_t tx_dropped;
    uint64_t rx_dropped;

    uint64_t ep_rx_dropped_bytes;
    DmUsbControlDevice control;
    /* Board adapter around the reusable DWC2 device-mode core. */
    DmUsbDwc2Device dwc2;
    qemu_irq irq;
    bool ep0_stalled;
    uint8_t test_rx[256];
    size_t test_rx_len;
    size_t test_rx_pos;
    uint8_t test_tx[4096];
    size_t test_tx_len;
    size_t test_tx_pos;
    bool test_tx_valid;
    uint8_t address;
    uint8_t configuration;
    bool configured;
    uint8_t line_coding[7];
    uint32_t control_line_state;
} DmMc02Usb;

bool dm_mc02_usb_init(DmMc02Usb *state, Object *owner, Chardev *chardev,
                      Error **errp);
void dm_mc02_usb_reset(DmMc02Usb *state);
void dm_mc02_usb_cleanup(DmMc02Usb *state);
void dm_mc02_usb_set_irq(DmMc02Usb *state, qemu_irq irq);

#endif
