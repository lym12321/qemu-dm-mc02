/* Board-independent adapter from a controller-owned SSI bus to dm-w25q64. */
#include "qemu/osdep.h"

#include "hw/arm/dm_mc02_ssi_nor.h"
#include "hw/block/dm_w25q64.h"
#include "hw/qdev-properties.h"
#include "qapi/error.h"

bool dm_mc02_ssi_nor_init(DmMc02SsiNor *adapter, DeviceState *parent,
                          const char *bus_name,
                          uint32_t expected_size,
                          const uint8_t expected_id[3], Error **errp)
{
    if (!adapter || !parent || !bus_name ||
        expected_size != DM_W25Q64_SIZE ||
        (expected_id && memcmp(expected_id, "\xef\x40\x17", 3))) {
        error_setg(errp, "invalid SSI NOR adapter configuration");
        return false;
    }
    memset(adapter, 0, sizeof(*adapter));
    adapter->bus = ssi_create_bus(parent, bus_name);
    adapter->device = qdev_new(TYPE_DM_W25Q64);
    qdev_prop_set_uint8(adapter->device, "cs", 0);
    if (!qdev_realize_and_unref(adapter->device, BUS(adapter->bus), errp)) {
        adapter->device = NULL;
        return false;
    }

    adapter->storage = dm_w25q64_storage(adapter->device);

    adapter->cs = qdev_get_gpio_in_named(adapter->device, SSI_GPIO_CS, 0);
    /* An active-low SSI peripheral starts selected until its CS input is
     * driven.  Force the first transition so the adapter's idle state is
     * unambiguously deasserted. */
    adapter->selected = true;
    dm_mc02_ssi_nor_select(adapter, false);
    return true;
}

void dm_mc02_ssi_nor_select(DmMc02SsiNor *adapter, bool selected)
{
    if (!adapter || !adapter->device || adapter->selected == selected) {
        return;
    }
    /* QEMU's active-low SSI CS input uses low for the selected state. */
    qemu_set_irq(adapter->cs, selected ? 0 : 1);
    adapter->selected = selected;
}

uint8_t dm_mc02_ssi_nor_transfer(DmMc02SsiNor *adapter, uint8_t value)
{
    if (!adapter || !adapter->bus || !adapter->selected) {
        return 0;
    }
    return ssi_transfer(adapter->bus, value) & 0xff;
}

void dm_mc02_ssi_nor_reset(DmMc02SsiNor *adapter)
{
    if (!adapter || !adapter->device) {
        return;
    }
    device_cold_reset(adapter->device);
    dm_mc02_ssi_nor_select(adapter, false);
}
