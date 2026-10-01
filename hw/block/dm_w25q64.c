/*
 * Board-independent W25Q64 functional subset, using QEMU's SSI transport.
 * Reference: Winbond W25Q64JV Rev J (2018-03-27), pp. 24-40, 49.
 * Operations complete synchronously on CS rising; no physical busy latency,
 * protection/QE registers, continuous-read mode, or migration is modeled.
 */
#include "qemu/osdep.h"
#include "hw/block/dm_w25q64.h"
#include "hw/ssi/ssi.h"
#include "migration/vmstate.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "../../../../cosim/dm_nor_flash.h"

OBJECT_DECLARE_SIMPLE_TYPE(DmW25q64, DM_W25Q64)

struct DmW25q64 {
    SSIPeripheral parent_obj;
    DmNorFlash flash;
    bool command_seen;
    bool invalid;
    bool have_data;
    uint8_t command;
    unsigned address_bytes;
    unsigned dummy;
    unsigned cursor;
    uint32_t address;
    uint8_t page[256];
};

static bool addressed(uint8_t cmd)
{
    switch (cmd) {
    case 0x02: case 0x32: case 0x03: case 0x0b: case 0x6b: case 0xeb:
    case 0x20: case 0x52: case 0xd8:
        return true;
    default:
        return false;
    }
}

static void transaction_reset(DmW25q64 *s)
{
    s->command_seen = s->invalid = s->have_data = false;
    s->command = 0;
    s->address_bytes = s->dummy = s->cursor = s->address = 0;
    memset(s->page, 0xff, sizeof(s->page));
}

static int set_cs(SSIPeripheral *dev, bool high)
{
    DmW25q64 *s = DM_W25Q64(dev);

    if (high && s->command_seen && !s->invalid &&
        (!addressed(s->command) || s->address_bytes == 3)) {
        switch (s->command) {
        case 0x06:
            dm_nor_flash_write_enable(&s->flash);
            break;
        case 0x04:
            s->flash.write_enable_latch = false;
            break;
        case 0x02: case 0x32:
            if (s->have_data) {
                /* The last byte sent to a page offset wins in the input
                 * latch; only the final latch is ANDed into NOR storage. */
                dm_nor_flash_page_program(&s->flash, s->address & ~255u,
                                         s->page, sizeof(s->page));
            }
            break;
        case 0x20:
            dm_nor_flash_sector_erase(&s->flash, s->address);
            break;
        case 0x52: case 0xd8:
            dm_nor_flash_erase(&s->flash, s->address,
                              s->command == 0x52 ? 32768 : 65536);
            break;
        case 0xc7: case 0x60:
            dm_nor_flash_chip_erase(&s->flash);
            break;
        }
    }
    transaction_reset(s);
    return 0;
}

static uint32_t transfer(SSIPeripheral *dev, uint32_t value)
{
    DmW25q64 *s = DM_W25Q64(dev);
    static const uint8_t id[] = { 0xef, 0x40, 0x17 };
    uint8_t result;

    if (!s->command_seen) {
        s->command_seen = true;
        s->command = value;
        switch (s->command) {
        case 0x06: case 0x04: case 0x05: case 0x9f:
        case 0x02: case 0x32: case 0x03: case 0x0b: case 0x6b: case 0xeb:
        case 0x20: case 0x52: case 0xd8: case 0xc7: case 0x60:
            break;
        default:
            s->invalid = true;
            qemu_log_mask(LOG_UNIMP, "dm-w25q64: unsupported command 0x%02x\n",
                          s->command);
        }
        return 0xff;
    }
    if (s->invalid) {
        return 0xff;
    }
    if (addressed(s->command) && s->address_bytes < 3) {
        s->address = (s->address << 8) | (value & 0xff);
        if (++s->address_bytes == 3) {
            s->address &= DM_W25Q64_SIZE - 1;
            s->cursor = s->address & 255;
            s->dummy = s->command == 0xeb ? 3 :
                       (s->command == 0x0b || s->command == 0x6b);
        }
        return 0xff;
    }
    if (s->dummy) {
        /* eb supports only the non-continuous Fxh mode profile. */
        if (s->command == 0xeb && s->dummy == 3 && (value & 0xf0) != 0xf0) {
            s->invalid = true;
            qemu_log_mask(LOG_UNIMP, "dm-w25q64: continuous read unsupported\n");
        }
        --s->dummy;
        return 0xff;
    }
    switch (s->command) {
    case 0x05:
        return dm_nor_flash_status(&s->flash);
    case 0x9f:
        result = s->cursor < sizeof(id) ? id[s->cursor] : 0xff;
        s->cursor = MIN(s->cursor + 1, sizeof(id));
        return result;
    case 0x03: case 0x0b: case 0x6b: case 0xeb:
        result = dm_nor_flash_read_byte(&s->flash, s->address);
        s->address = (s->address + 1) & (DM_W25Q64_SIZE - 1);
        return result;
    case 0x02: case 0x32:
        s->page[s->cursor] = value;
        s->cursor = (s->cursor + 1) & 255;
        s->have_data = true;
        return 0xff;
    default:
        /* Extra bytes invalidate fixed-length mutating instructions. */
        s->invalid = true;
        return 0xff;
    }
}

static void reset(DeviceState *dev)
{
    DmW25q64 *s = DM_W25Q64(dev);
    transaction_reset(s);
    dm_nor_flash_reset(&s->flash);
}

static void realize(SSIPeripheral *dev, Error **errp)
{
    DmW25q64 *s = DM_W25Q64(dev);
    uint8_t *storage = g_malloc(DM_W25Q64_SIZE);

    memset(storage, 0xff, DM_W25Q64_SIZE);
    g_assert(dm_nor_flash_init(&s->flash, storage, DM_W25Q64_SIZE, 256, 4096));
    transaction_reset(s);
}

static void finalize(Object *obj)
{
    g_free(DM_W25Q64(obj)->flash.storage);
}

uint8_t *dm_w25q64_storage(DeviceState *dev)
{
    return DM_W25Q64(dev)->flash.storage;
}

uint32_t dm_w25q64_size(DeviceState *dev)
{
    return DM_W25Q64(dev)->flash.storage_size;
}

static const VMStateDescription vmstate = {
    .name = TYPE_DM_W25Q64,
    .unmigratable = 1,
};

static void class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    SSIPeripheralClass *sc = SSI_PERIPHERAL_CLASS(klass);

    dc->reset = reset;
    dc->vmsd = &vmstate;
    sc->realize = realize;
    sc->transfer = transfer;
    sc->set_cs = set_cs;
    sc->cs_polarity = SSI_CS_LOW;
}

static const TypeInfo info = {
    .name = TYPE_DM_W25Q64,
    .parent = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(DmW25q64),
    .instance_finalize = finalize,
    .class_init = class_init,
};

static void register_types(void)
{
    type_register_static(&info);
}
type_init(register_types)
