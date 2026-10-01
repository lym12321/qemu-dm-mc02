/* Focused contract test for the reusable message-RAM owner. */
#include "qemu/osdep.h"
#include "hw/misc/dm_message_ram.h"

static void test_init_data_and_reset(void)
{
    DmMessageRam ram;
    uint8_t *data;

    g_assert_true(dm_message_ram_init(&ram, NULL, "test-dm-message-ram",
                                      0x100, &error_abort));
    g_assert_cmpuint(dm_message_ram_size(&ram), ==, 0x100);
    g_assert_true(dm_message_ram_region(&ram) == &ram.region);
    g_assert_true(dm_message_ram_data(&ram) != NULL);
    g_assert_true(dm_message_ram_const_data(&ram) == ram.data);

    data = dm_message_ram_data(&ram);
    memset(data, 0xa5, 0x100);
    dm_message_ram_reset(&ram);
    for (unsigned i = 0; i < 0x100; ++i) {
        g_assert_cmpuint(data[i], ==, 0);
    }
}

static void test_invalid_configuration_is_rejected(void)
{
    DmMessageRam ram;

    g_assert_false(dm_message_ram_init(&ram, NULL, "test-dm-message-ram",
                                       0, NULL));
    g_assert_false(dm_message_ram_init(&ram, NULL, NULL, 0x100, NULL));
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-message-ram/init-reset", test_init_data_and_reset);
    g_test_add_func("/dm-message-ram/reject-invalid",
                    test_invalid_configuration_is_rejected);
    return g_test_run();
}
