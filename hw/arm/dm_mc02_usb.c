/* STM32H723 device USB wiring. All packet/register behavior is in the
 * board-independent controller; descriptors and class policy belong to guest. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_usb.h"
#define __packed QEMU_PACKED
#include "hw/usb/dwc2-regs.h"
#undef __packed

static void usb_irq(void *opaque, bool level)
{
    DmMc02Usb *s = opaque;
    if (s->irq) { qemu_set_irq(s->irq, level); }
}

static bool endpoint_valid(hwaddr offset)
{
    if (offset >= 0x1000 && offset < 0x10000) {
        return (offset - 0x1000) / 0x1000 < 9;
    }
    if (offset >= 0x900 && offset < 0xb00) {
        return (offset - 0x900) / 0x20 < 9;
    }
    if (offset >= 0xb00 && offset < 0xd00) {
        return (offset - 0xb00) / 0x20 < 9;
    }
    return true;
}

static uint64_t usb_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Usb *s = opaque;
    /* Synthesized capabilities, RM0468 Rev3 Table531: EP0 + 8 IN/OUT,
     * dedicated FIFOs and 4 KiB SRAM. Not a measured silicon reset dump. */
    const uint32_t config[] = { 0,
        (1u << 19) | (8u << 10) | (1u << 8) | (2u << 3) | 4u,
        1024u << 16, (8u << 26) | GHWCFG4_DED_FIFO_EN | (1u << 16) };
    if (offset >= 0x44 && offset + size <= 0x54 && !(offset & 3)) {
        return config[(offset - 0x44) / 4] & (size == 4 ? UINT32_MAX : (1u << (size * 8)) - 1);
    }
    return endpoint_valid(offset) ? dm_usb_dwc2_read(&s->dwc2, offset, size) : 0;
}

static void usb_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    DmMc02Usb *s = opaque;
    if (offset >= 0x44 && offset < 0x54) { return; }
    if (offset == DM_USB_DWC2_GRSTCTL && (value & 1)) {
        dm_usb_bridge_reset(s->bridge);
    }
    if (endpoint_valid(offset)) { dm_usb_dwc2_write(&s->dwc2, offset, value, size); }
}

static const MemoryRegionOps usb_ops = {
    .read = usb_read, .write = usb_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 4, .impl.unaligned = true,
};

bool dm_mc02_usb_init(DmMc02Usb *s, Object *owner, Chardev *chr, Error **errp)
{
    memset(s, 0, sizeof(*s));
    dm_usb_dwc2_init(&s->dwc2, NULL, usb_irq, s, 64, 9);
    memory_region_init_io(&s->iomem, owner, &usb_ops, s,
                          "dm-mc02.usb-otg-hs", DM_MC02_USB_REGION_SIZE);
    if (chr) {
        s->bridge = dm_usb_bridge_new(&s->dwc2, 9, chr, errp);
        if (!s->bridge) { return false; }
    }
    return true;
}

void dm_mc02_usb_reset(DmMc02Usb *s)
{
    dm_usb_bridge_reset(s->bridge);
    dm_usb_dwc2_reset(&s->dwc2);
}
void dm_mc02_usb_cleanup(DmMc02Usb *s) { dm_usb_bridge_free(s->bridge); s->bridge = NULL; }
void dm_mc02_usb_set_irq(DmMc02Usb *s, qemu_irq irq) { s->irq = irq; }
