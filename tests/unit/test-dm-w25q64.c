/* Isolated real QOM/SSI consumer of the project-owned NOR device. */
#include "qemu/osdep.h"
#include "hw/block/dm_w25q64.h"
#include "hw/arm/dm_mc02_ssi_nor.h"
#include "hw/ssi/ssi.h"
#include "hw/irq.h"
#include "qapi/error.h"
#include "qemu/module.h"

static DeviceState *parent, *flash;
static SSIBus *bus;
static qemu_irq cs;

static void select_flash(bool selected)
{
    qemu_set_irq(cs, !selected);
}

static uint8_t byte(uint8_t value)
{
    return ssi_transfer(bus, value);
}

static void command(uint8_t op)
{
    select_flash(true);
    byte(op);
}

static void address(uint32_t addr)
{
    byte(addr >> 16);
    byte(addr >> 8);
    byte(addr);
}

static void enable(void)
{
    command(0x06);
    select_flash(false);
}

static uint8_t status(void)
{
    command(0x05);
    uint8_t result = byte(0);
    select_flash(false);
    return result;
}

static void setup(void)
{
    parent = qdev_new("dm-nor-test-parent");
    bus = ssi_create_bus(parent, "nor");
    flash = qdev_new(TYPE_DM_W25Q64);
    qdev_realize_and_unref(flash, BUS(bus), &error_abort);
    cs = qdev_get_gpio_in_named(flash, SSI_GPIO_CS, 0);
    select_flash(false);
}

static void teardown(void)
{
    object_unref(OBJECT(parent));
}

static void test_identification(void)
{
    setup();
    g_assert_cmpuint(dm_w25q64_size(flash), ==, 8 * 1024 * 1024);
    command(0x9f);
    g_assert_cmphex(byte(0), ==, 0xef);
    g_assert_cmphex(byte(0), ==, 0x40);
    g_assert_cmphex(byte(0), ==, 0x17);
    g_assert_cmphex(byte(0), ==, 0xff);
    select_flash(false);
    command(0x03);
    address(0x7fffff);
    g_assert_cmphex(byte(0), ==, 0xff);
    g_assert_cmphex(byte(0), ==, 0xff);
    select_flash(false);
    g_assert_cmphex(status(), ==, 0);
    teardown();
}

static void test_program(void)
{
    setup();
    uint8_t *storage = dm_w25q64_storage(flash);
    command(0x02); address(255); byte(0x12); select_flash(false);
    g_assert_cmphex(storage[255], ==, 0xff); /* WREN required */
    enable();
    g_assert_cmphex(status(), ==, 2);
    command(0x02); address(255); byte(0x12); byte(0x34);
    g_assert_cmphex(storage[255], ==, 0xff); /* no early commit */
    select_flash(false);
    g_assert_cmphex(storage[255], ==, 0x12);
    g_assert_cmphex(storage[0], ==, 0x34);
    g_assert_cmphex(storage[256], ==, 0xff);
    g_assert_cmphex(status(), ==, 0);
    enable(); command(0x32); address(255); byte(0xf0); select_flash(false);
    g_assert_cmphex(storage[255], ==, 0x10); /* 1 -> 0 only */
    /* More than one page replaces input latch bytes before programming. */
    enable(); command(0x02); address(512);
    for (unsigned i = 0; i < 256; ++i) { byte(0); }
    byte(0xaa); select_flash(false);
    g_assert_cmphex(storage[512], ==, 0xaa);
    g_assert_cmphex(storage[513], ==, 0);
    teardown();
}

static void test_erase(void)
{
    setup();
    uint8_t *storage = dm_w25q64_storage(flash);
    const uint8_t ops[] = { 0x20, 0x52, 0xd8 };
    const unsigned sizes[] = { 4096, 32768, 65536 };
    for (unsigned i = 0; i < 3; ++i) {
        memset(storage, 0, sizes[i] * 3);
        enable(); command(ops[i]); address(sizes[i] + 123);
        g_assert_cmphex(storage[sizes[i]], ==, 0);
        select_flash(false);
        g_assert_cmphex(storage[sizes[i] - 1], ==, 0);
        for (unsigned j = sizes[i]; j < sizes[i] * 2; ++j) {
            g_assert_cmphex(storage[j], ==, 0xff);
        }
        g_assert_cmphex(storage[sizes[i] * 2], ==, 0);
        g_assert_cmphex(status(), ==, 0);
    }
    enable(); command(0xc7); select_flash(false);
    for (unsigned i = 0; i < DM_W25Q64_SIZE; ++i) {
        g_assert_cmphex(storage[i], ==, 0xff);
    }
    teardown();
}

static void test_reads(void)
{
    setup();
    uint8_t *storage = dm_w25q64_storage(flash);
    storage[DM_W25Q64_SIZE - 1] = 0x12; storage[0] = 0x34;
    const uint8_t ops[] = { 0x03, 0x0b, 0x6b, 0xeb };
    for (unsigned i = 0; i < 4; ++i) {
        command(ops[i]); address(0x7fffff);
        if (ops[i] == 0xeb) { byte(0xf0); byte(0); byte(0); }
        else if (ops[i] != 0x03) { byte(0); }
        g_assert_cmphex(byte(0), ==, 0x12);
        g_assert_cmphex(byte(0), ==, 0x34);
        select_flash(false);
    }
    teardown();
}

static void test_abort_reset(void)
{
    setup();
    uint8_t *storage = dm_w25q64_storage(flash);
    storage[0] = 0x12;
    enable(); command(0x20); byte(0); byte(0); select_flash(false);
    g_assert_cmphex(storage[0], ==, 0x12);
    g_assert_cmphex(status(), ==, 2);
    command(0x20); address(0); byte(0); select_flash(false);
    g_assert_cmphex(storage[0], ==, 0x12); /* extra erase byte rejected */
    command(0x02); address(0); select_flash(false);
    g_assert_cmphex(status(), ==, 2); /* zero-length program */
    command(0xfe); byte(0); select_flash(false);
    g_assert_cmphex(status(), ==, 2);
    command(0x02); address(0); byte(0);
    device_cold_reset(flash); select_flash(false);
    g_assert_cmphex(storage[0], ==, 0x12);
    g_assert_cmphex(status(), ==, 0);
    enable(); command(0x04); select_flash(false);
    g_assert_cmphex(status(), ==, 0);
    teardown();
}

static void test_adapter(void)
{
    DmMc02SsiNor adapter = { 0 };
    const uint8_t id[] = { 0xef, 0x40, 0x17 };
    Error *err = NULL;
    parent = qdev_new("dm-nor-test-parent");
    g_assert_false(dm_mc02_ssi_nor_init(&adapter, parent, "nor",
                                      DM_W25Q64_SIZE / 2, id, &err));
    g_assert_nonnull(err);
    error_free(err);
    g_assert_null(adapter.device);
    err = NULL;
    const uint8_t wrong_id[] = { 0xef, 0x40, 0x18 };
    g_assert_false(dm_mc02_ssi_nor_init(&adapter, parent, "nor",
                                      DM_W25Q64_SIZE, wrong_id, &err));
    g_assert_nonnull(err);
    error_free(err);
    g_assert_null(adapter.device);
    g_assert_true(dm_mc02_ssi_nor_init(&adapter, parent, "nor",
                                     DM_W25Q64_SIZE, id, &error_abort));
    bus = adapter.bus; flash = adapter.device;
    /* Reset via the consumer must abort before its CS deassertion. */
    dm_mc02_ssi_nor_select(&adapter, true);
    dm_mc02_ssi_nor_transfer(&adapter, 0x06);
    dm_mc02_ssi_nor_select(&adapter, false);
    dm_mc02_ssi_nor_select(&adapter, true);
    dm_mc02_ssi_nor_transfer(&adapter, 0x02);
    address(0); byte(0);
    dm_mc02_ssi_nor_reset(&adapter);
    g_assert_false(adapter.selected);
    g_assert_cmphex(adapter.storage[0], ==, 0xff);
    cs = adapter.cs;
    g_assert_cmphex(status(), ==, 0);
    teardown();
}

int main(int argc, char **argv)
{
    static const TypeInfo parent_info = {
        .name = "dm-nor-test-parent", .parent = TYPE_DEVICE,
        .instance_size = sizeof(DeviceState),
    };
    g_test_init(&argc, &argv, NULL);
    module_call_init(MODULE_INIT_QOM);
    type_register_static(&parent_info);
    g_test_add_func("/dm-w25q64/id", test_identification);
    g_test_add_func("/dm-w25q64/program", test_program);
    g_test_add_func("/dm-w25q64/erase", test_erase);
    g_test_add_func("/dm-w25q64/read", test_reads);
    g_test_add_func("/dm-w25q64/abort-reset", test_abort_reset);
    g_test_add_func("/dm-w25q64/adapter", test_adapter);
    return g_test_run();
}
