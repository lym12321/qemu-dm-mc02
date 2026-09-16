/* H723 SOF status IRQ handler adapter for a periodic endpoint registry. */
#include "dm_stm32h7_usb_host_sof_irq.h"

#define OTG_GINTSTS    0x014u
#define GINTSTS_SOF    (1u << 3)

bool dm_stm32h7_usb_host_sof_irq_init(
    DmStm32H7UsbHostSofIrq *irq, uintptr_t base,
    DmStm32H7UsbHostPeriodicRegistrySof *event)
{
    if (!irq || !event) {
        return false;
    }
    *irq = (DmStm32H7UsbHostSofIrq) {
        .base = base,
        .event = event,
    };
    return true;
}

unsigned dm_stm32h7_usb_host_sof_irq_handle(DmStm32H7UsbHostSofIrq *irq)
{
    volatile uint32_t *gintsts = (volatile uint32_t *)(irq->base +
                                                        OTG_GINTSTS);

    if (!(*gintsts & GINTSTS_SOF)) {
        return 0;
    }
    *gintsts = GINTSTS_SOF;
    return dm_stm32h7_usb_host_periodic_registry_sof_on_event(irq->event);
}
