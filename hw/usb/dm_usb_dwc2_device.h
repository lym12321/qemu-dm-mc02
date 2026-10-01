/* Board-independent minimal DWC2 device-mode controller. */
#ifndef DM_USB_DWC2_DEVICE_H
#define DM_USB_DWC2_DEVICE_H

#include "hw/usb/dm_usb_transaction.h"
#include "qemu/typedefs.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_USB_DWC2_MAX_ENDPOINTS 16
#define DM_USB_DWC2_DIEPTXF_COUNT (DM_USB_DWC2_MAX_ENDPOINTS - 1)
#define DM_USB_DWC2_FIFO_BYTES 4096
#define DM_USB_DWC2_FIFO0 0x1000
#define DM_USB_DWC2_FIFO_STRIDE 0x1000

#define DM_USB_DWC2_GAHBCFG 0x008
#define DM_USB_DWC2_GRSTCTL 0x010
#define DM_USB_DWC2_GINTSTS 0x014
#define DM_USB_DWC2_GINTMSK 0x018
#define DM_USB_DWC2_GRXFSIZ 0x024
#define DM_USB_DWC2_GNPTXFSIZ 0x028
#define DM_USB_DWC2_DIEPTXF0 0x100
#define DM_USB_DWC2_DIEPTXF_STRIDE 0x4
#define DM_USB_DWC2_GSNPSID 0x040
#define DM_USB_DWC2_DCFG 0x800
#define DM_USB_DWC2_DCTL 0x804
#define DM_USB_DWC2_DSTS 0x808
#define DM_USB_DWC2_DIEPMSK 0x810
#define DM_USB_DWC2_DOEPMSK 0x814
#define DM_USB_DWC2_DAINT 0x818
#define DM_USB_DWC2_DAINTMSK 0x81c
#define DM_USB_DWC2_DIEPEMPMSK 0x834
#define DM_USB_DWC2_DIEPCTL0 0x900
#define DM_USB_DWC2_DIEPINT0 0x908
#define DM_USB_DWC2_DIEPTSIZ0 0x910
#define DM_USB_DWC2_DIEPDMA0 0x914
#define DM_USB_DWC2_DTXFSTS0 0x918
#define DM_USB_DWC2_DOEPCTL0 0xb00
#define DM_USB_DWC2_DOEPINT0 0xb08
#define DM_USB_DWC2_DOEPTSIZ0 0xb10
#define DM_USB_DWC2_DOEPDMA0 0xb14

#define DM_USB_DWC2_EP_STRIDE 0x20
#define DM_USB_DWC2_GINTSTS_RXFLVL (1u << 4)
#define DM_USB_DWC2_GINTSTS_IEPINT (1u << 18)
#define DM_USB_DWC2_GINTSTS_OEPINT (1u << 19)
#define DM_USB_DWC2_GAHBCFG_GINT (1u << 0)
#define DM_USB_DWC2_GRSTCTL_AHBIDL (1u << 31)
#define DM_USB_DWC2_GRSTCTL_CSFTRST (1u << 0)
#define DM_USB_DWC2_GRSTCTL_CSFTRSTDONE (1u << 29)
#define DM_USB_DWC2_DXEPCTL_EPENA (1u << 31)
#define DM_USB_DWC2_DXEPCTL_EPDIS (1u << 30)
#define DM_USB_DWC2_DXEPCTL_STALL (1u << 21)
#define DM_USB_DWC2_DXEPCTL_EPTYPE_MASK (3u << 18)
#define DM_USB_DWC2_DXEPCTL_USBAEP (1u << 15)
#define DM_USB_DWC2_DXEPCTL_MPS_MASK 0x7ffu
#define DM_USB_DWC2_DCFG_DAD_SHIFT 4
#define DM_USB_DWC2_DCFG_DAD_MASK (0x7fu << DM_USB_DWC2_DCFG_DAD_SHIFT)
#define DM_USB_DXEPINT_XFRC (1u << 0)
#define DM_USB_DXEPINT_STUP (1u << 3)
#define DM_USB_DXEPTSIZ_PKTCNT_MASK (0x3ffu << 19)
#define DM_USB_DXEPTSIZ_XFERSIZE_MASK 0x7ffffu
#define DM_USB_DXEPTSIZ_PKTCNT_SHIFT 19

typedef void DmUsbDwc2Irq(void *opaque, bool level);

typedef struct DmUsbDwc2Endpoint {
    uint32_t in_ctl;
    uint32_t out_ctl;
    uint32_t in_int;
    uint32_t out_int;
    uint32_t in_tsiz;
    uint32_t out_tsiz;
    uint32_t in_dma;
    uint32_t out_dma;
    uint8_t in_fifo[DM_USB_DWC2_FIFO_BYTES];
    uint8_t out_fifo[DM_USB_DWC2_FIFO_BYTES];
    size_t in_fifo_head;
    size_t in_fifo_count;
    size_t out_fifo_head;
    size_t out_fifo_count;
} DmUsbDwc2Endpoint;

typedef struct DmUsbDwc2Device {
    DmUsbControlDevice *control;
    DmUsbTransactionDevice transaction;
    DmUsbDwc2Irq *irq;
    void *irq_opaque;
    uint16_t ep0_max_packet_size;
    uint32_t gahbcfg;
    uint32_t grstctl;
    uint32_t gintsts;
    uint32_t gintmsk;
    uint32_t grxfsiz;
    uint32_t gnptxfsiz;
    uint32_t dieptxf[DM_USB_DWC2_DIEPTXF_COUNT];
    uint32_t dcfg;
    uint32_t dctl;
    uint32_t dsts;
    uint32_t diepmsk;
    uint32_t doepmsk;
    uint32_t daintmsk;
    uint32_t diepempmsk;
    bool irq_level;
    uint64_t fifo_overflow;
    DmUsbDwc2Endpoint endpoint[DM_USB_DWC2_MAX_ENDPOINTS];
} DmUsbDwc2Device;

void dm_usb_dwc2_init(DmUsbDwc2Device *device,
                      DmUsbControlDevice *control,
                      DmUsbDwc2Irq *irq, void *irq_opaque,
                      uint16_t ep0_max_packet_size);
void dm_usb_dwc2_reset(DmUsbDwc2Device *device);
void dm_usb_dwc2_bus_reset(DmUsbDwc2Device *device, uint64_t timestamp_ns);

DmUsbTransactionResult dm_usb_dwc2_submit(
    DmUsbDwc2Device *device, const DmUsbTransaction *transaction);

/* Query pending bytes without exposing endpoint implementation state. */
bool dm_usb_dwc2_endpoint_fifo_count(const DmUsbDwc2Device *device,
                                     unsigned endpoint, bool in,
                                     size_t *count);

/* Rebind destination-owned transaction callbacks and rebuild the derived
 * level-sensitive IRQ projection after a component state restore. */
void dm_usb_dwc2_sync_runtime(DmUsbDwc2Device *device);

/* Component-only state contract.  The control device, IRQ callback and
 * endpoint callback wiring are destination-owned and are not serialized. */
bool dm_usb_dwc2_state_valid(const DmUsbDwc2Device *device);
const VMStateDescription *dm_usb_dwc2_vmstate(void);
const VMStateDescription *dm_usb_dwc2_vmstate_raw(void);

/* Child description used by an enclosing composite state boundary. */
extern const VMStateDescription vmstate_dm_usb_dwc2_raw;

/* FIFO reads consume the selected endpoint's OUT FIFO. */
uint64_t dm_usb_dwc2_read(DmUsbDwc2Device *device,
                          uint32_t offset, unsigned size);
void dm_usb_dwc2_write(DmUsbDwc2Device *device,
                       uint32_t offset, uint64_t value, unsigned size);

#endif /* DM_USB_DWC2_DEVICE_H */
