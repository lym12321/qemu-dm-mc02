/* Tests for the board-independent USB host-port lifecycle boundary. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_port.h"

typedef struct TestUsbHostPort {
    unsigned reset_calls;
    void *reset_opaque;
    uint64_t reset_timestamp_ns;
} TestUsbHostPort;

static void test_reset(void *opaque, uint64_t timestamp_ns)
{
    TestUsbHostPort *test = opaque;

    ++test->reset_calls;
    test->reset_opaque = opaque;
    test->reset_timestamp_ns = timestamp_ns;
}

static void test_callback_timestamp_and_opaque(void)
{
    TestUsbHostPort test = { 0 };
    DmUsbHostPort port;

    dm_usb_host_port_init(&port, test_reset, &test);
    dm_usb_host_port_reset(&port, 123456789);
    g_assert_cmpuint(test.reset_calls, ==, 1);
    g_assert_true(test.reset_opaque == &test);
    g_assert_cmpuint(test.reset_timestamp_ns, ==, 123456789);
}

static void test_missing_callback_is_noop(void)
{
    DmUsbHostPort port;

    dm_usb_host_port_init(&port, NULL, NULL);
    dm_usb_host_port_reset(&port, 1);
    dm_usb_host_port_reset(NULL, 2);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/host-port/reset-callback",
                    test_callback_timestamp_and_opaque);
    g_test_add_func("/dm-usb/host-port/reset-noop",
                    test_missing_callback_is_noop);
    return g_test_run();
}
