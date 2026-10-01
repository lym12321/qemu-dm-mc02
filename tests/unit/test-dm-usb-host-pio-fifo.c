/* Tests for the board-independent host PIO FIFO. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_pio_fifo.h"

static void test_order_and_exact_read(void)
{
    DmUsbHostPioFifo fifo = { 0 };
    uint8_t data[] = { 1, 2, 3, 4 };
    uint8_t output[4] = { 0 };

    g_assert_cmpuint(dm_usb_host_pio_fifo_write(&fifo, data, sizeof(data)), ==,
                     sizeof(data));
    g_assert_cmpuint(dm_usb_host_pio_fifo_count(&fifo), ==, sizeof(data));
    g_assert_true(dm_usb_host_pio_fifo_read_exact(&fifo, output,
                                                  sizeof(output)));
    g_assert_cmpmem(output, sizeof(output), data, sizeof(data));
    g_assert_cmpuint(dm_usb_host_pio_fifo_count(&fifo), ==, 0);
}

static void test_partial_read_is_non_destructive_for_exact(void)
{
    DmUsbHostPioFifo fifo = { 0 };
    uint8_t data[] = { 9, 8, 7 };
    uint8_t output[4] = { 0 };

    dm_usb_host_pio_fifo_write(&fifo, data, sizeof(data));
    g_assert_false(dm_usb_host_pio_fifo_read_exact(&fifo, output,
                                                   sizeof(output)));
    g_assert_cmpuint(dm_usb_host_pio_fifo_count(&fifo), ==, sizeof(data));
    g_assert_cmpuint(dm_usb_host_pio_fifo_read(&fifo, output, sizeof(output)),
                     ==, sizeof(data));
    g_assert_cmpmem(output, sizeof(data), data, sizeof(data));
}

static void test_wrap_and_capacity(void)
{
    DmUsbHostPioFifo fifo = { 0 };
    uint8_t input[DM_USB_HOST_PIO_FIFO_CAPACITY + 1];
    uint8_t output[2];

    memset(input, 0xa5, sizeof(input));
    g_assert_cmpuint(dm_usb_host_pio_fifo_write(&fifo, input, sizeof(input)), ==,
                     DM_USB_HOST_PIO_FIFO_CAPACITY);
    g_assert_cmpuint(dm_usb_host_pio_fifo_read(&fifo, output, sizeof(output)), ==,
                     sizeof(output));
    g_assert_cmpuint(dm_usb_host_pio_fifo_write(&fifo, input, sizeof(output)), ==,
                     sizeof(output));
    g_assert_cmpuint(dm_usb_host_pio_fifo_count(&fifo), ==,
                     DM_USB_HOST_PIO_FIFO_CAPACITY);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/host-pio-fifo/order-exact-read",
                    test_order_and_exact_read);
    g_test_add_func("/dm-usb/host-pio-fifo/exact-non-destructive",
                    test_partial_read_is_non_destructive_for_exact);
    g_test_add_func("/dm-usb/host-pio-fifo/wrap-capacity",
                    test_wrap_and_capacity);
    return g_test_run();
}
