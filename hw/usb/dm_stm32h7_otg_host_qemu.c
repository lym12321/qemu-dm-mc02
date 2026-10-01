/* QEMU composition for one reusable STM32H7 DWC2 host controller. */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "exec/address-spaces.h"
#include "hw/irq.h"
#include "hw/qdev-core.h"
#include "hw/usb.h"
#include "hw/usb/dm_stm32h7_otg_host_qemu.h"
#include "hw/usb/dm_stm32h7_otg_host.h"
#include "hw/usb/dm_usb_host_channel_data_path.h"
#include "hw/usb/dm_usb_host_channel_transport.h"
#include "hw/usb/dm_usb_host_qemu_memory.h"
#include "hw/usb/dm_usb_host_qemu_port.h"
#include "hw/usb/dm_usb_host_qemu_transport.h"
#include "hw/usb/dm_usb_host_qemu_completion_scheduler.h"
#include "hw/qdev-properties.h"

struct DmStm32H7OtgHostQemu {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    DmStm32H7OtgHost host;
    DmUsbHostChannelDataPath data_path;
    DmUsbHostChannelTransport channel_transport;
    DmUsbHostQemuMemory memory;
    DmUsbHostQemuTransport transport;
    DmUsbHostQemuPort port;
    DmUsbHostQemuCompletionScheduler completion_scheduler;
    USBBus bus;
    QEMUTimer *sof_timer;
    bool completion_scheduler_enabled;
    uint64_t completion_delay_ns;
    bool bus_created;
};

static void dm_stm32h7_otg_host_qemu_rearm(
    DmStm32H7OtgHostQemu *s)
{
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    dm_stm32h7_otg_host_advance_time(&s->host, now);
    if (s->host.sof_running) {
        timer_mod(s->sof_timer, s->host.next_sof_ns);
    } else {
        timer_del(s->sof_timer);
    }
}

static void dm_stm32h7_otg_host_qemu_sof(void *opaque)
{
    dm_stm32h7_otg_host_qemu_rearm(opaque);
}

static void dm_stm32h7_otg_host_qemu_irq(void *opaque, bool level)
{
    DmStm32H7OtgHostQemu *s = opaque;

    qemu_set_irq(s->irq, level);
}

static DmStm32H7OtgPortSpeed dm_stm32h7_otg_host_qemu_port_speed(
    const USBDevice *device)
{
    switch (device->speed) {
    case USB_SPEED_LOW:
        return DM_STM32H7_OTG_PORT_LOW_SPEED;
    case USB_SPEED_HIGH:
        return DM_STM32H7_OTG_PORT_HIGH_SPEED;
    default:
        return DM_STM32H7_OTG_PORT_FULL_SPEED;
    }
}

static bool dm_stm32h7_otg_host_qemu_fifo_channel(hwaddr offset,
                                                  unsigned *channel)
{
    if (offset < DM_STM32H7_OTG_HCFIFO(0) || offset % 0x1000u) {
        return false;
    }
    *channel = offset / 0x1000u - 1u;
    return *channel < DM_STM32H7_OTG_HOST_CHANNELS;
}

static uint64_t dm_stm32h7_otg_host_qemu_read(void *opaque, hwaddr offset,
                                               unsigned size)
{
    DmStm32H7OtgHostQemu *s = opaque;
    unsigned channel;

    dm_stm32h7_otg_host_qemu_rearm(s);
    if (dm_stm32h7_otg_host_qemu_fifo_channel(offset, &channel)) {
        return dm_stm32h7_otg_host_fifo_read(&s->host, channel, size);
    }
    return dm_stm32h7_otg_host_read(&s->host, offset);
}

static void dm_stm32h7_otg_host_qemu_write(void *opaque, hwaddr offset,
                                            uint64_t value, unsigned size)
{
    DmStm32H7OtgHostQemu *s = opaque;
    unsigned channel;

    dm_stm32h7_otg_host_qemu_rearm(s);
    if (dm_stm32h7_otg_host_qemu_fifo_channel(offset, &channel)) {
        dm_stm32h7_otg_host_fifo_write(&s->host, channel, value, size);
    } else {
        dm_stm32h7_otg_host_write(&s->host, offset, value,
                                   qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
    dm_stm32h7_otg_host_qemu_rearm(s);
}

static const MemoryRegionOps dm_stm32h7_otg_host_qemu_ops = {
    .read = dm_stm32h7_otg_host_qemu_read,
    .write = dm_stm32h7_otg_host_qemu_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void dm_stm32h7_otg_host_qemu_reset(DeviceState *dev)
{
    DmStm32H7OtgHostQemu *s = DM_STM32H7_OTG_HOST_QEMU(dev);

    timer_del(s->sof_timer);
    if (s->completion_scheduler_enabled) {
        dm_usb_host_qemu_completion_scheduler_reset(
            &s->completion_scheduler);
    }
    dm_stm32h7_otg_host_reset(&s->host);
    if (s->port.port.dev && s->port.port.dev->attached) {
        dm_stm32h7_otg_host_set_port_connected(
            &s->host, true,
            dm_stm32h7_otg_host_qemu_port_speed(s->port.port.dev));
    }
}

static void dm_stm32h7_otg_host_qemu_realize(DeviceState *dev,
                                              Error **errp)
{
    DmStm32H7OtgHostQemu *s = DM_STM32H7_OTG_HOST_QEMU(dev);
    static USBBusOps bus_ops = {
    };

    (void)errp;
    dm_stm32h7_otg_host_init(&s->host, dm_usb_host_qemu_port_reset,
                              &s->port, dm_stm32h7_otg_host_qemu_irq, s);
    dm_usb_host_qemu_memory_init(&s->memory, &address_space_memory);
    dm_usb_host_channel_data_path_init(
        &s->data_path, &s->host, dm_usb_host_qemu_memory_read,
        dm_usb_host_qemu_memory_write, &s->memory);
    dm_usb_host_channel_transport_init_with_opaques(
        &s->channel_transport, &s->host, NULL, NULL,
        dm_usb_host_channel_data_path_read_out, &s->data_path,
        dm_usb_host_channel_data_path_write_in, &s->data_path);

    usb_bus_new(&s->bus, sizeof(s->bus), &bus_ops, dev);
    s->bus_created = true;
    dm_usb_host_qemu_port_init(&s->port, &s->bus, &s->host, 0);
    dm_usb_host_qemu_transport_init_port(&s->transport, &s->port.port);
    dm_usb_host_channel_transport_set_route(
        &s->channel_transport, dm_usb_host_qemu_transport_route,
        &s->transport);
    if (s->completion_scheduler_enabled) {
        g_assert_true(dm_usb_host_qemu_completion_scheduler_init(
            &s->completion_scheduler, s->completion_delay_ns));
        dm_stm32h7_otg_host_set_channel_cancel(
            &s->host, dm_usb_host_qemu_completion_scheduler_cancel,
            &s->completion_scheduler);
        dm_usb_host_channel_transport_set_completion_scheduler(
            &s->channel_transport,
            dm_usb_host_qemu_completion_scheduler_schedule,
            &s->completion_scheduler);
    }
    s->sof_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                dm_stm32h7_otg_host_qemu_sof, s);
}

static void dm_stm32h7_otg_host_qemu_unrealize(DeviceState *dev)
{
    DmStm32H7OtgHostQemu *s = DM_STM32H7_OTG_HOST_QEMU(dev);

    if (s->completion_scheduler_enabled) {
        dm_stm32h7_otg_host_set_channel_cancel(&s->host, NULL, NULL);
        dm_usb_host_channel_transport_set_completion_scheduler(
            &s->channel_transport, NULL, NULL);
        dm_usb_host_qemu_completion_scheduler_destroy(
            &s->completion_scheduler);
    }
    /* The channel transport borrows both the route adapter and its USB
     * port.  Break that graph before releasing the port/bus objects. */
    dm_usb_host_channel_transport_set_route(&s->channel_transport, NULL,
                                             NULL);
    dm_usb_host_qemu_transport_clear(&s->transport);
    timer_free(s->sof_timer);
    s->sof_timer = NULL;
    dm_usb_host_qemu_port_cleanup(&s->port);
    if (s->bus_created) {
        usb_bus_release(&s->bus);
        s->bus_created = false;
    }
}

static void dm_stm32h7_otg_host_qemu_init(Object *obj)
{
    DmStm32H7OtgHostQemu *s = DM_STM32H7_OTG_HOST_QEMU(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &dm_stm32h7_otg_host_qemu_ops, s,
                          "stm32h7-otg-host", DM_STM32H7_OTG_HOST_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void dm_stm32h7_otg_host_qemu_class_init(ObjectClass *klass,
                                                 void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    static Property properties[] = {
        DEFINE_PROP_BOOL("completion-scheduler", DmStm32H7OtgHostQemu,
                         completion_scheduler_enabled, false),
        DEFINE_PROP_UINT64("completion-delay-ns", DmStm32H7OtgHostQemu,
                           completion_delay_ns, 0),
        DEFINE_PROP_END_OF_LIST(),
    };

    (void)data;
    dc->realize = dm_stm32h7_otg_host_qemu_realize;
    dc->unrealize = dm_stm32h7_otg_host_qemu_unrealize;
    dc->reset = dm_stm32h7_otg_host_qemu_reset;
    device_class_set_props(dc, properties);
    set_bit(DEVICE_CATEGORY_USB, dc->categories);
}

static const TypeInfo dm_stm32h7_otg_host_qemu_type_info = {
    .name = TYPE_DM_STM32H7_OTG_HOST_QEMU,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(DmStm32H7OtgHostQemu),
    .instance_init = dm_stm32h7_otg_host_qemu_init,
    .class_init = dm_stm32h7_otg_host_qemu_class_init,
};

static void dm_stm32h7_otg_host_qemu_register_types(void)
{
    type_register_static(&dm_stm32h7_otg_host_qemu_type_info);
}

type_init(dm_stm32h7_otg_host_qemu_register_types)
