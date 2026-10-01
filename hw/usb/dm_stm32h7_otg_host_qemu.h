/* QEMU SysBus composition for the reusable STM32H7 USB host controller. */
#ifndef HW_USB_DM_STM32H7_OTG_HOST_QEMU_H
#define HW_USB_DM_STM32H7_OTG_HOST_QEMU_H

#include "hw/sysbus.h"

#define TYPE_DM_STM32H7_OTG_HOST_QEMU "dm-stm32h7-otg-host-qemu"
OBJECT_DECLARE_SIMPLE_TYPE(DmStm32H7OtgHostQemu,
                           DM_STM32H7_OTG_HOST_QEMU)

/* HCFIFO channel 11 begins at 0xc000 and occupies its final word. */
#define DM_STM32H7_OTG_HOST_MMIO_SIZE 0xd000u

#endif /* HW_USB_DM_STM32H7_OTG_HOST_QEMU_H */
