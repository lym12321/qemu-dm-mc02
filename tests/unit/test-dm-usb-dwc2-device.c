/* Tests for the board-independent minimal DWC2 device-mode controller. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_dwc2_device.h"
#include "hw/usb/dm_usb_host.h"

typedef struct TestDwc2 {
    DmUsbControlDevice control;
    DmUsbDwc2Device device;
    DmUsbHost host;
    uint8_t descriptor[18];
    uint8_t address;
    uint8_t configuration;
    bool irq_level;
    unsigned irq_changes;
} TestDwc2;

static bool test_get_descriptor(void *opaque, uint8_t type, uint8_t index,
                                const uint8_t **data, size_t *length)
{
    TestDwc2 *test = opaque;

    if (type != 1 || index != 0) {
        return false;
    }
    *data = test->descriptor;
    *length = sizeof(test->descriptor);
    return true;
}

static void test_irq(void *opaque, bool level)
{
    TestDwc2 *test = opaque;

    test->irq_level = level;
    ++test->irq_changes;
}

static void test_set_address(void *opaque, uint8_t address)
{
    ((TestDwc2 *)opaque)->address = address;
}

static void test_set_configuration(void *opaque, uint8_t configuration)
{
    ((TestDwc2 *)opaque)->configuration = configuration;
}

static DmUsbTransactionResult test_host_submit(
    void *opaque, const DmUsbTransaction *transaction)
{
    TestDwc2 *test = opaque;

    return dm_usb_dwc2_submit(&test->device, transaction);
}

static void test_init(TestDwc2 *test, uint16_t max_packet_size)
{
    static const DmUsbControlOps control_ops = {
        .get_descriptor = test_get_descriptor,
        .set_address = test_set_address,
        .set_configuration = test_set_configuration,
    };

    memset(test, 0, sizeof(*test));
    for (unsigned i = 0; i < sizeof(test->descriptor); ++i) {
        test->descriptor[i] = (uint8_t)(i + 1);
    }
    dm_usb_control_init(&test->control, &control_ops, test,
                        max_packet_size);
    dm_usb_dwc2_init(&test->device, &test->control, test_irq, test,
                     max_packet_size);
    dm_usb_host_init(&test->host, test_host_submit, test, max_packet_size);
}

static uint32_t endpoint_register(uint32_t base, unsigned endpoint)
{
    return base + endpoint * DM_USB_DWC2_EP_STRIDE;
}

static uint32_t endpoint_control(unsigned endpoint)
{
    return DM_USB_DWC2_DXEPCTL_USBAEP |
           (2u << 18) |
           4u |
           DM_USB_DWC2_DXEPCTL_EPENA;
}

static uint32_t endpoint_transfer_size(size_t length, unsigned packets)
{
    return (uint32_t)length |
           ((uint32_t)packets << DM_USB_DXEPTSIZ_PKTCNT_SHIFT);
}

static void enable_endpoint_irq(TestDwc2 *test, unsigned endpoint,
                                 bool in)
{
    dm_usb_dwc2_write(&test->device, DM_USB_DWC2_GAHBCFG,
                      DM_USB_DWC2_GAHBCFG_GINT, 4);
    dm_usb_dwc2_write(&test->device, DM_USB_DWC2_GINTMSK,
                      in ? DM_USB_DWC2_GINTSTS_IEPINT :
                           DM_USB_DWC2_GINTSTS_OEPINT, 4);
    dm_usb_dwc2_write(&test->device, in ? DM_USB_DWC2_DIEPMSK :
                      DM_USB_DWC2_DOEPMSK, DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&test->device, DM_USB_DWC2_DAINTMSK,
                      in ? 1u << endpoint : 1u << (16 + endpoint), 4);
}

static void test_reset_register_defaults(void)
{
    TestDwc2 test;

    test_init(&test, 4);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GAHBCFG, 4),
                     ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GRSTCTL, 4),
                     ==, DM_USB_DWC2_GRSTCTL_AHBIDL);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GRXFSIZ, 4),
                     ==, DM_USB_DWC2_FIFO_BYTES / 4);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GINTSTS, 4),
                     ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_DAINT, 4),
                     ==, 0);
    g_assert_false(test.irq_level);
}

static void test_endpoint_fifo_count_query(void)
{
    static const uint8_t out_data[] = { 'x', 'y', 'z' };
    TestDwc2 test;
    size_t count = SIZE_MAX;
    uint32_t in_ctl = endpoint_register(DM_USB_DWC2_DIEPCTL0, 1);
    uint32_t in_tsiz = endpoint_register(DM_USB_DWC2_DIEPTSIZ0, 1);
    uint32_t out_ctl = endpoint_register(DM_USB_DWC2_DOEPCTL0, 1);
    uint32_t out_tsiz = endpoint_register(DM_USB_DWC2_DOEPTSIZ0, 1);
    uint32_t fifo = DM_USB_DWC2_FIFO0 + DM_USB_DWC2_FIFO_STRIDE;
    DmUsbHostResult result;

    test_init(&test, 4);
    g_assert_false(dm_usb_dwc2_endpoint_fifo_count(NULL, 1, true, &count));
    g_assert_false(dm_usb_dwc2_endpoint_fifo_count(&test.device,
                                                    DM_USB_DWC2_MAX_ENDPOINTS,
                                                    true, &count));
    g_assert_false(dm_usb_dwc2_endpoint_fifo_count(&test.device, 1, true,
                                                    NULL));
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&test.device, 1, true,
                                                  &count));
    g_assert_cmpuint(count, ==, 0);
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&test.device, 1, false,
                                                  &count));
    g_assert_cmpuint(count, ==, 0);

    dm_usb_dwc2_write(&test.device, in_ctl, endpoint_control(1), 4);
    dm_usb_dwc2_write(&test.device, in_tsiz, endpoint_transfer_size(2, 1), 4);
    dm_usb_dwc2_write(&test.device, fifo, 0x00004241, 2);
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&test.device, 1, true,
                                                  &count));
    g_assert_cmpuint(count, ==, 2);
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&test.device, 1, true,
                                                  &count));
    g_assert_cmpuint(count, ==, 2);

    dm_usb_dwc2_write(&test.device, out_ctl, endpoint_control(1), 4);
    dm_usb_dwc2_write(&test.device, out_tsiz,
                      endpoint_transfer_size(sizeof(out_data), 1), 4);
    result = dm_usb_host_bulk_out(&test.host, 1, 4, out_data,
                                  sizeof(out_data), 123);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&test.device, 1, false,
                                                  &count));
    g_assert_cmpuint(count, ==, sizeof(out_data));
}

static void test_bulk_in_fifo_and_irq(void)
{
    TestDwc2 test;
    uint8_t data[6];
    DmUsbHostResult result;
    uint32_t ctl = endpoint_register(DM_USB_DWC2_DIEPCTL0, 1);
    uint32_t tsiz = endpoint_register(DM_USB_DWC2_DIEPTSIZ0, 1);
    uint32_t intr = endpoint_register(DM_USB_DWC2_DIEPINT0, 1);
    uint32_t fifo = DM_USB_DWC2_FIFO0 + DM_USB_DWC2_FIFO_STRIDE;

    test_init(&test, 4);
    enable_endpoint_irq(&test, 1, true);
    dm_usb_dwc2_write(&test.device, ctl, endpoint_control(1), 4);
    dm_usb_dwc2_write(&test.device, tsiz, endpoint_transfer_size(6, 2), 4);
    dm_usb_dwc2_write(&test.device, fifo, 0x44434241, 4);
    dm_usb_dwc2_write(&test.device, fifo, 0x00004645, 2);

    result = dm_usb_host_bulk_in(&test.host, 1, 4, data, sizeof(data),
                                 123456);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(data));
    g_assert_cmpuint(result.transactions, ==, 2);
    g_assert_cmpmem(data, sizeof(data), "ABCDEF", 6);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, tsiz, 4), ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, intr, 4), ==,
                     DM_USB_DXEPINT_XFRC);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_DAINT, 4),
                     ==, 1u << 1);
    g_assert_true(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GINTSTS, 4) &
                  DM_USB_DWC2_GINTSTS_IEPINT);
    g_assert_true(test.irq_level);

    dm_usb_dwc2_write(&test.device, intr, DM_USB_DXEPINT_XFRC, 4);
    g_assert_false(test.irq_level);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GINTSTS, 4) &
                     DM_USB_DWC2_GINTSTS_IEPINT, ==, 0);
}

static void test_bulk_out_fifo_and_irq(void)
{
    static const uint8_t expected[] = "uvwxyz";
    TestDwc2 test;
    uint8_t data[6];
    DmUsbHostResult result;
    uint32_t ctl = endpoint_register(DM_USB_DWC2_DOEPCTL0, 1);
    uint32_t tsiz = endpoint_register(DM_USB_DWC2_DOEPTSIZ0, 1);
    uint32_t intr = endpoint_register(DM_USB_DWC2_DOEPINT0, 1);
    uint32_t fifo = DM_USB_DWC2_FIFO0 + DM_USB_DWC2_FIFO_STRIDE;

    test_init(&test, 4);
    enable_endpoint_irq(&test, 1, false);
    dm_usb_dwc2_write(&test.device, ctl, endpoint_control(1), 4);
    dm_usb_dwc2_write(&test.device, tsiz, endpoint_transfer_size(6, 2), 4);

    result = dm_usb_host_bulk_out(&test.host, 1, 4, expected, sizeof(expected) - 1,
                                  654321);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(expected) - 1);
    g_assert_cmpuint(result.transactions, ==, 2);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, tsiz, 4), ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, intr, 4), ==,
                     DM_USB_DXEPINT_XFRC);
    for (unsigned i = 0; i < sizeof(data); i += 4) {
        uint32_t word = dm_usb_dwc2_read(&test.device, fifo, 4);

        data[i] = word & 0xff;
        data[i + 1] = (word >> 8) & 0xff;
        if (i + 2 < sizeof(data)) {
            data[i + 2] = (word >> 16) & 0xff;
            data[i + 3] = (word >> 24) & 0xff;
        }
    }
    g_assert_cmpmem(data, sizeof(data), expected, sizeof(data));
    g_assert_true(test.irq_level);

    dm_usb_dwc2_write(&test.device, intr, DM_USB_DXEPINT_XFRC, 4);
    g_assert_false(test.irq_level);
}

static void test_control_descriptor_and_setup_interrupt(void)
{
    static const uint8_t setup[] = { 0x80, 6, 0, 1, 0, 0, 18, 0 };
    TestDwc2 test;
    uint8_t data[18];
    DmUsbHostResult result;
    uint32_t intr = DM_USB_DWC2_DOEPINT0;

    test_init(&test, 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_GAHBCFG,
                      DM_USB_DWC2_GAHBCFG_GINT, 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_GINTMSK,
                      DM_USB_DWC2_GINTSTS_IEPINT |
                      DM_USB_DWC2_GINTSTS_OEPINT, 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_DIEPMSK,
                      DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_DOEPMSK,
                      DM_USB_DXEPINT_STUP | DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_DAINTMSK, 1u | (1u << 16), 4);

    result = dm_usb_host_control_transfer(&test.host, setup, NULL, 0, data,
                                          sizeof(data), 777);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(data));
    g_assert_cmpuint(result.transactions, ==, 7);
    g_assert_cmpmem(data, sizeof(data), test.descriptor,
                    sizeof(test.descriptor));
    g_assert_cmpint(dm_usb_control_phase(&test.control), ==,
                    DM_USB_CONTROL_IDLE);
    g_assert_true(dm_usb_dwc2_read(&test.device, intr, 4) &
                  DM_USB_DXEPINT_STUP);
    g_assert_true(test.irq_level);

    dm_usb_dwc2_write(&test.device, intr,
                      DM_USB_DXEPINT_STUP | DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_DIEPINT0,
                      DM_USB_DXEPINT_XFRC, 4);
    g_assert_false(test.irq_level);
}

static void test_interrupt_mask_w1c_and_reset_deassert(void)
{
    TestDwc2 test;
    uint32_t ctl = endpoint_register(DM_USB_DWC2_DIEPCTL0, 1);
    uint32_t tsiz = endpoint_register(DM_USB_DWC2_DIEPTSIZ0, 1);
    uint32_t intr = endpoint_register(DM_USB_DWC2_DIEPINT0, 1);
    uint32_t fifo = DM_USB_DWC2_FIFO0 + DM_USB_DWC2_FIFO_STRIDE;
    unsigned changes;

    test_init(&test, 4);
    enable_endpoint_irq(&test, 1, true);
    dm_usb_dwc2_write(&test.device, ctl, endpoint_control(1), 4);
    dm_usb_dwc2_write(&test.device, tsiz, endpoint_transfer_size(1, 1), 4);
    dm_usb_dwc2_write(&test.device, fifo, 'X', 1);
    g_assert_cmpint(dm_usb_host_bulk_in(&test.host, 1, 4, NULL, 0, 1).status,
                    ==, DM_USB_TRANSACTION_INVALID);
    g_assert_false(test.irq_level);

    dm_usb_dwc2_write(&test.device, fifo, 'Y', 1);
    {
        uint8_t data;
        DmUsbHostResult result = dm_usb_host_bulk_in(&test.host, 1, 4,
                                                     &data, 1, 1);
        g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
        g_assert_cmpuint(data, ==, 'X');
    }
    g_assert_true(test.irq_level);
    dm_usb_dwc2_write(&test.device, intr + 1, 0xff, 2);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, intr, 4), ==,
                     DM_USB_DXEPINT_XFRC);
    dm_usb_dwc2_write(&test.device, intr, DM_USB_DXEPINT_XFRC, 4);
    g_assert_false(test.irq_level);

    dm_usb_dwc2_write(&test.device, intr + 3, 0xffff, 2);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, intr, 4), ==, 0);
    dm_usb_dwc2_write(&test.device, ctl, endpoint_control(1), 4);
    dm_usb_dwc2_write(&test.device, tsiz, endpoint_transfer_size(1, 1), 4);
    dm_usb_dwc2_write(&test.device, ctl, endpoint_control(1), 3);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, ctl, 4), ==,
                     endpoint_control(1));

    dm_usb_dwc2_write(&test.device, fifo, 'Z', 1);
    {
        uint8_t data;
        DmUsbHostResult result = dm_usb_host_bulk_in(&test.host, 1, 4,
                                                     &data, 1, 1);
        g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
        g_assert_cmpuint(data, ==, 'Y');
    }
    g_assert_true(test.irq_level);
    changes = test.irq_changes;
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_GRSTCTL,
                      DM_USB_DWC2_GRSTCTL_CSFTRST, 4);
    g_assert_false(test.irq_level);
    g_assert_cmpuint(test.irq_changes, ==, changes + 1);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GRSTCTL, 4),
                     ==, DM_USB_DWC2_GRSTCTL_AHBIDL |
                         DM_USB_DWC2_GRSTCTL_CSFTRSTDONE);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, ctl, 4), ==, 0);
}

static void test_bus_reset(void)
{
    static const uint8_t set_address[] = { 0, 5, 42, 0, 0, 0, 0, 0 };
    static const uint8_t set_configuration[] = { 0, 9, 3, 0, 0, 0, 0, 0 };
    static const uint8_t descriptor[] = { 0x80, 6, 0, 1, 0, 0, 18, 0 };
    TestDwc2 test;
    uint8_t data;
    DmUsbHostResult result;
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_SETUP,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = 0,
        .out_data = descriptor,
        .length = sizeof(descriptor),
        .timestamp_ns = 1234,
    };
    uint32_t ctl = endpoint_register(DM_USB_DWC2_DIEPCTL0, 1);
    uint32_t intr = endpoint_register(DM_USB_DWC2_DIEPINT0, 1);
    uint32_t tsiz = endpoint_register(DM_USB_DWC2_DIEPTSIZ0, 1);
    uint32_t dma = endpoint_register(DM_USB_DWC2_DIEPDMA0, 1);
    uint32_t fifo = DM_USB_DWC2_FIFO0 + DM_USB_DWC2_FIFO_STRIDE;

    test_init(&test, 4);
    result = dm_usb_host_control_transfer(&test.host, set_address, NULL, 0,
                                          NULL, 0, 1);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    result = dm_usb_host_control_transfer(&test.host, set_configuration,
                                          NULL, 0, NULL, 0, 2);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(test.address, ==, 42);
    g_assert_cmpuint(test.configuration, ==, 3);
    g_assert_cmpint(dm_usb_dwc2_submit(&test.device, &transaction).status, ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_control_phase(&test.control), ==,
                    DM_USB_CONTROL_DATA_IN);

    dm_usb_dwc2_write(&test.device, ctl, endpoint_control(1), 4);
    dm_usb_dwc2_write(&test.device, tsiz, endpoint_transfer_size(1, 1), 4);
    dm_usb_dwc2_write(&test.device, fifo, 'X', 1);
    result = dm_usb_host_bulk_in(&test.host, 1, 4, &data, 1, 3);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_transaction_next_pid(&test.device.transaction, 1,
                                                 DM_USB_ENDPOINT_IN), ==,
                    DM_USB_TRANSACTION_PID_DATA1);

    dm_usb_dwc2_write(&test.device, ctl,
                      endpoint_control(1) | DM_USB_DWC2_DXEPCTL_STALL, 4);
    dm_usb_dwc2_write(&test.device, tsiz, endpoint_transfer_size(1, 1), 4);
    dm_usb_dwc2_write(&test.device, fifo, 'Y', 1);
    result = dm_usb_host_bulk_in(&test.host, 1, 4, &data, 1, 4);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_STALL);
    g_assert_true(dm_usb_transaction_halted(&test.device.transaction, 1,
                                            DM_USB_ENDPOINT_IN));

    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_DCFG,
                      2 | (42u << DM_USB_DWC2_DCFG_DAD_SHIFT), 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_GRXFSIZ, 256, 4);
    dm_usb_dwc2_write(&test.device, dma, 0x11223344, 4);
    dm_usb_dwc2_bus_reset(&test.device, 987654321);

    g_assert_cmpint(dm_usb_control_phase(&test.control), ==,
                    DM_USB_CONTROL_IDLE);
    g_assert_cmpuint(dm_usb_control_address(&test.control), ==, 0);
    g_assert_cmpuint(dm_usb_control_configuration(&test.control), ==, 0);
    g_assert_cmpuint(test.address, ==, 0);
    g_assert_cmpuint(test.configuration, ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_DCFG, 4), ==,
                     2);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_GRXFSIZ, 4),
                     ==, 256);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, dma, 4), ==, 0x11223344);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, ctl, 4), ==,
                     endpoint_control(1) & ~(DM_USB_DWC2_DXEPCTL_EPENA |
                                             DM_USB_DWC2_DXEPCTL_STALL));
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, tsiz, 4), ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, intr, 4), ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device,
                                      DM_USB_DWC2_DTXFSTS0 +
                                      DM_USB_DWC2_EP_STRIDE, 4), ==,
                     DM_USB_DWC2_FIFO_BYTES / 4);
    g_assert_false(dm_usb_transaction_halted(&test.device.transaction, 1,
                                              DM_USB_ENDPOINT_IN));
    g_assert_cmpint(dm_usb_transaction_next_pid(&test.device.transaction, 1,
                                                 DM_USB_ENDPOINT_IN), ==,
                    DM_USB_TRANSACTION_PID_DATA0);
    result = dm_usb_host_bulk_in(&test.host, 1, 4, &data, 1, 5);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NAK);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/dwc2/reset-defaults",
                    test_reset_register_defaults);
    g_test_add_func("/dm-usb/dwc2/endpoint-fifo-count-query",
                    test_endpoint_fifo_count_query);
    g_test_add_func("/dm-usb/dwc2/bulk-in-fifo-irq",
                    test_bulk_in_fifo_and_irq);
    g_test_add_func("/dm-usb/dwc2/bulk-out-fifo-irq",
                    test_bulk_out_fifo_and_irq);
    g_test_add_func("/dm-usb/dwc2/control-descriptor",
                    test_control_descriptor_and_setup_interrupt);
    g_test_add_func("/dm-usb/dwc2/mask-w1c-reset",
                    test_interrupt_mask_w1c_and_reset_deassert);
    g_test_add_func("/dm-usb/dwc2/bus-reset", test_bus_reset);
    return g_test_run();
}
