/* Minimal STM32H723 host-role reference profile. */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "exec/address-spaces.h"
#include "hw/arm/armv7m.h"
#include "hw/arm/dm_mc02_soc.h"
#include "hw/arm/boot.h"
#include "hw/boards.h"
#include "hw/core/cpu.h"
#include "hw/sysbus.h"
#include "hw/usb/dm_stm32h7_otg_host_qemu.h"
#include "hw/qdev-clock.h"
#include "hw/qdev-properties.h"

#define TYPE_STM32H723_USB_HOST_MACHINE \
    MACHINE_TYPE_NAME("stm32h723-usb-host")
OBJECT_DECLARE_SIMPLE_TYPE(Stm32H723UsbHostMachineState,
                           STM32H723_USB_HOST_MACHINE)

struct Stm32H723UsbHostMachineState {
    MachineState parent_obj;

    DmMc02SocMemory soc_memory;
    DeviceState *armv7m;
    DeviceState *usb_host;
    Clock *sysclk;
    Clock *refclk;
};

static void stm32h723_usb_host_init(MachineState *machine)
{
    Stm32H723UsbHostMachineState *s =
        STM32H723_USB_HOST_MACHINE(machine);
    const DmMc02SocProfile *soc = dm_mc02_soc_stm32h723();
    MemoryRegion *system_memory = get_system_memory();

    dm_mc02_soc_memory_init(&s->soc_memory, OBJECT(machine), system_memory,
                            soc, &error_fatal);
    s->sysclk = clock_new(OBJECT(machine), "SYSCLK");
    clock_set_hz(s->sysclk, soc->reset_cpu_hz);
    s->refclk = clock_new(OBJECT(machine), "REFCLK");
    clock_set_hz(s->refclk, soc->refclk_hz);

    s->armv7m = qdev_new(TYPE_ARMV7M);
    object_property_add_child(OBJECT(machine), "armv7m", OBJECT(s->armv7m));
    qdev_prop_set_uint32(s->armv7m, "num-irq", soc->irq_count);
    qdev_prop_set_string(s->armv7m, "cpu-type", soc->cpu_type);
    qdev_prop_set_uint32(s->armv7m, "init-nsvtor", soc->flash_base);
    qdev_prop_set_bit(s->armv7m, "enable-bitband", false);
    qdev_connect_clock_in(s->armv7m, "cpuclk", s->sysclk);
    qdev_connect_clock_in(s->armv7m, "refclk", s->refclk);
    object_property_set_link(OBJECT(s->armv7m), "memory",
                             OBJECT(system_memory), &error_abort);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(s->armv7m), &error_fatal);

    s->usb_host = qdev_new(TYPE_DM_STM32H7_OTG_HOST_QEMU);
    object_property_add_child(OBJECT(machine), "usb-host",
                              OBJECT(s->usb_host));
    qdev_prop_set_bit(s->usb_host, "completion-scheduler", true);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(s->usb_host), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(s->usb_host), 0, soc->usb_hs_base);
    sysbus_connect_irq(SYS_BUS_DEVICE(s->usb_host), 0,
                       qdev_get_gpio_in(s->armv7m, 77));

    armv7m_load_kernel(ARM_CPU(first_cpu), machine->kernel_filename,
                       soc->flash_base, soc->flash_size);
}

static void stm32h723_usb_host_machine_class_init(ObjectClass *oc,
                                                   void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    static const char * const valid_cpu_types[] = {
        ARM_CPU_TYPE_NAME("cortex-m7"),
        NULL,
    };

    (void)data;
    mc->desc = "STM32H723 USB host-role reference profile";
    mc->init = stm32h723_usb_host_init;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("cortex-m7");
    mc->valid_cpu_types = valid_cpu_types;
    mc->default_ram_size = 0;
    mc->max_cpus = 1;
}

static const TypeInfo stm32h723_usb_host_machine_type = {
    .name = TYPE_STM32H723_USB_HOST_MACHINE,
    .parent = TYPE_MACHINE,
    .instance_size = sizeof(Stm32H723UsbHostMachineState),
    .class_init = stm32h723_usb_host_machine_class_init,
};

static void stm32h723_usb_host_machine_register_types(void)
{
    type_register_static(&stm32h723_usb_host_machine_type);
}

type_init(stm32h723_usb_host_machine_register_types)
