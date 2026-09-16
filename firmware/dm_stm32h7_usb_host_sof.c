/* STM32H7 register source for controller-independent USB SOF virtual time. */
#include "dm_stm32h7_usb_host_sof.h"

#define OTG_HFNUM             0x408u
#define OTG_HPRT0             0x440u
#define HFNUM_FRNUM_MASK      0xffffu
#define HPRT0_SPD_SHIFT       17u
#define HPRT0_SPD_MASK        (3u << HPRT0_SPD_SHIFT)

static volatile uint32_t *dm_stm32h7_usb_host_sof_reg(
    const DmStm32H7UsbHostSof *source, uint32_t offset)
{
    return (volatile uint32_t *)(source->base + offset);
}

static DmUsbHostSpeed dm_stm32h7_usb_host_sof_speed(uint32_t hprt0)
{
    switch ((hprt0 & HPRT0_SPD_MASK) >> HPRT0_SPD_SHIFT) {
    case 0:
        return DM_USB_HOST_SPEED_HIGH;
    case 2:
        return DM_USB_HOST_SPEED_LOW;
    default:
        return DM_USB_HOST_SPEED_FULL;
    }
}

DmUsbHostSofClockResult dm_stm32h7_usb_host_sof_init(
    DmStm32H7UsbHostSof *source, uintptr_t base, uint64_t origin_ns)
{
    uint32_t hprt0;
    uint32_t hfnum;

    if (!source) {
        return DM_USB_HOST_SOF_CLOCK_INVALID;
    }
    source->base = base;
    hprt0 = *dm_stm32h7_usb_host_sof_reg(source, OTG_HPRT0);
    hfnum = *dm_stm32h7_usb_host_sof_reg(source, OTG_HFNUM);
    return dm_usb_host_sof_clock_init(
        &source->clock, dm_stm32h7_usb_host_sof_speed(hprt0),
        hfnum & HFNUM_FRNUM_MASK, origin_ns);
}

uint64_t dm_stm32h7_usb_host_sof_timestamp(DmStm32H7UsbHostSof *source)
{
    uint32_t hfnum = *dm_stm32h7_usb_host_sof_reg(source, OTG_HFNUM);

    return dm_usb_host_sof_clock_advance(&source->clock,
                                         hfnum & HFNUM_FRNUM_MASK);
}
