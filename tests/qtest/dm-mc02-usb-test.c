/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"

#define USB_BASE       0x40040000
#define USB_DCFG       (USB_BASE + 0x800)
#define USB_DCFG_DAD_SHIFT 4
#define USB_DCFG_DAD_MASK (0x7fu << USB_DCFG_DAD_SHIFT)
#define USB_GINTSTS    (USB_BASE + 0x014)
#define USB_DAINT      (USB_BASE + 0x818)
#define USB_DIEPINT0   (USB_BASE + 0x908)
#define USB_DOEPINT0   (USB_BASE + 0xb08)
#define USB_FIFO1      (USB_BASE + 0x2000)
#define USB_FIFO2      (USB_BASE + 0x3000)
#define USB_FIFO3      (USB_BASE + 0x4000)
#define USB_FIFO4      (USB_BASE + 0x5000)
#define USB_FIFO5      (USB_BASE + 0x6000)
#define TEST_RX_LEN   (USB_BASE + 0x1f000)
#define TEST_RX_DATA  (USB_BASE + 0x1f004)
#define TEST_RX_SUBMIT (USB_BASE + 0x1f008)
#define TEST_TX_LEN   (USB_BASE + 0x1f010)
#define TEST_TX_DATA  (USB_BASE + 0x1f014)
#define TEST_STATUS   (USB_BASE + 0x1f018)
#define TEST_EP1_RX_COUNT (USB_BASE + 0x1f01c)
#define USB_GAHBCFG   (USB_BASE + 0x008)
#define USB_GINTMSK   (USB_BASE + 0x018)
#define USB_DIEPTXF0  (USB_BASE + 0x100)
#define USB_DIEPTXF1  (USB_BASE + 0x104)
#define TEST_IN       (1u << 8)
#define TEST_SETUP    (1u << 9)
#define TEST_EP0_STALLED (1u << 19)
#define USB_GINT_IEPINT (1u << 18)
#define USB_GINT_OEPINT (1u << 19)
#define NVIC_ISPR2     0xe000e208
#define NVIC_ICPR2     0xe000e288
#define USB_IRQ_NUMBER 77
#define USB_IRQ_BIT    (1u << (USB_IRQ_NUMBER - 64))

static QTestState *qts;

static void submit_packet(unsigned ep, bool in, bool setup,
                          const uint8_t *data, size_t len)
{
    size_t i;

    g_assert_cmpuint(len, <=, 256);
    qtest_writel(qts, TEST_RX_LEN, len);
    for (i = 0; i + sizeof(uint32_t) <= len; i += sizeof(uint32_t)) {
        uint32_t word = data[i] | ((uint32_t)data[i + 1] << 8) |
                        ((uint32_t)data[i + 2] << 16) |
                        ((uint32_t)data[i + 3] << 24);
        qtest_writel(qts, TEST_RX_DATA, word);
    }
    for (; i < len; ++i) {
        qtest_writeb(qts, TEST_RX_DATA, data[i]);
    }
    qtest_writel(qts, TEST_RX_SUBMIT, ep | (in ? TEST_IN : 0) |
                 (setup ? TEST_SETUP : 0));
}

static size_t receive_packet(uint8_t *data, size_t capacity)
{
    size_t len = qtest_readl(qts, TEST_TX_LEN);
    size_t i;

    g_assert_true(qtest_readl(qts, TEST_STATUS) & (1u << 17));
    g_assert_cmpuint(len, <=, capacity);
    for (i = 0; i < len; ++i) {
        data[i] = qtest_readb(qts, TEST_TX_DATA);
    }
    return len;
}

static void reset_machine(void)
{
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
}

static size_t get_descriptor(uint8_t type, uint8_t index, uint8_t *data,
                             size_t capacity)
{
    uint8_t setup[] = {
        0x80, 6, index, type, 0, 0, (uint8_t)capacity,
        (uint8_t)(capacity >> 8),
    };
    size_t total = 0;

    g_assert_cmpuint(capacity, <=, 256);
    submit_packet(0, false, true, setup, sizeof(setup));
    do {
        uint8_t packet[64];
        size_t len;

        submit_packet(0, true, false, NULL, 0);
        len = receive_packet(packet, sizeof(packet));
        g_assert_cmpuint(total + len, <=, capacity);
        memcpy(data + total, packet, len);
        total += len;
        if (len < sizeof(packet)) {
            break;
        }
    } while (total < capacity);
    return total;
}

static void test_descriptor_contract(void)
{
    uint8_t device[64];
    uint8_t config[128];
    size_t device_len;
    size_t config_len;
    size_t offset;
    bool seen_cdc_in = false;
    bool seen_cdc_out = false;
    bool seen_cdc_int = false;
    bool seen_winusb_in = false;
    bool seen_winusb_out = false;
    unsigned endpoint_count = 0;

    reset_machine();
    device_len = get_descriptor(1, 0, device, sizeof(device));
    g_assert_cmpuint(device_len, ==, 18);
    g_assert_cmpuint(device[0], ==, 18);
    g_assert_cmpuint(device[1], ==, 1);
    g_assert_cmpuint(device[2] | ((uint16_t)device[3] << 8), ==, 0x0210);
    g_assert_cmpuint(device[4], ==, 0xef);
    g_assert_cmpuint(device[5], ==, 0x02);
    g_assert_cmpuint(device[6], ==, 0x01);
    g_assert_cmpuint(device[8] | ((uint16_t)device[9] << 8), ==, 0x0483);
    g_assert_cmpuint(device[10] | ((uint16_t)device[11] << 8), ==, 0x5741);
    g_assert_cmpuint(device[12] | ((uint16_t)device[13] << 8), ==, 0x0101);

    config_len = get_descriptor(2, 0, config, sizeof(config));
    g_assert_cmpuint(config_len, ==, 98);
    g_assert_cmpuint(config[0], ==, 9);
    g_assert_cmpuint(config[1], ==, 2);
    g_assert_cmpuint(config[2] | ((uint16_t)config[3] << 8), ==, config_len);
    g_assert_cmpuint(config[4], ==, 3);

    for (offset = 0; offset < config_len;) {
        size_t length = config[offset];

        g_assert_cmpuint(length, >=, 2);
        g_assert_cmpuint(offset + length, <=, config_len);
        if (config[offset + 1] == 5) {
            g_assert_cmpuint(length, ==, 7);
            endpoint_count++;
            switch (config[offset + 2]) {
            case 0x81: seen_cdc_in = true; break;
            case 0x02: seen_cdc_out = true; break;
            case 0x83: seen_cdc_int = true; break;
            case 0x84: seen_winusb_in = true; break;
            case 0x05: seen_winusb_out = true; break;
            default: g_assert_not_reached();
            }
        }
        offset += length;
    }
    g_assert_cmpuint(endpoint_count, ==, 5);
    g_assert_true(seen_cdc_in);
    g_assert_true(seen_cdc_out);
    g_assert_true(seen_cdc_int);
    g_assert_true(seen_winusb_in);
    g_assert_true(seen_winusb_out);
}

static void test_power_on_tx_fifo_registers(void)
{
    const uint32_t dieptxf0_initial = 0x11223344;
    const uint32_t dieptxf1_initial = 0x55667788;

    reset_machine();

    /* CherryUSB validates each power-on FIFO depth before programming the
     * dynamic layout.  These registers belong to the reusable DWC2 core,
     * rather than the board's legacy compatibility register array. */
    g_assert_cmpuint(qtest_readl(qts, USB_DIEPTXF0) >> 16, >=, 256);
    g_assert_cmpuint(qtest_readl(qts, USB_DIEPTXF1) >> 16, >=, 256);

    qtest_writel(qts, USB_DIEPTXF1, 0x01230040);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF1), ==, 0x01230040);

    /* Aligned byte and half-word writes are readable from the corresponding
     * low lanes; writes to DIEPTXF1 must not alias DIEPTXF0. */
    qtest_writel(qts, USB_DIEPTXF0, dieptxf0_initial);
    qtest_writel(qts, USB_DIEPTXF1, dieptxf1_initial);

    qtest_writeb(qts, USB_DIEPTXF0, 0xa0);
    g_assert_cmphex(qtest_readb(qts, USB_DIEPTXF0), ==, 0xa0);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF0), ==, 0x112233a0);
    qtest_writeb(qts, USB_DIEPTXF1, 0xe4);
    g_assert_cmphex(qtest_readb(qts, USB_DIEPTXF1), ==, 0xe4);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF1), ==, 0x556677e4);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF0), ==, 0x112233a0);

    qtest_writel(qts, USB_DIEPTXF0, dieptxf0_initial);
    qtest_writel(qts, USB_DIEPTXF1, dieptxf1_initial);
    qtest_writew(qts, USB_DIEPTXF0, 0xa1b2);
    g_assert_cmphex(qtest_readw(qts, USB_DIEPTXF0), ==, 0xa1b2);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF0), ==, 0x1122a1b2);
    qtest_writew(qts, USB_DIEPTXF1, 0xe5f6);
    g_assert_cmphex(qtest_readw(qts, USB_DIEPTXF1), ==, 0xe5f6);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF1), ==, 0x5566e5f6);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF0), ==, 0x1122a1b2);

    /* Unaligned and cross-register accesses are ignored as a whole. */
    qtest_writew(qts, USB_DIEPTXF0 + 1, 0xffff);
    qtest_writew(qts, USB_DIEPTXF0 + 3, 0xffff);
    qtest_writel(qts, USB_DIEPTXF0 + 2, 0xffffffff);
    qtest_writew(qts, USB_DIEPTXF1 + 1, 0xffff);
    qtest_writew(qts, USB_DIEPTXF1 + 3, 0xffff);
    qtest_writel(qts, USB_DIEPTXF1 + 2, 0xffffffff);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF0), ==, 0x1122a1b2);
    g_assert_cmphex(qtest_readl(qts, USB_DIEPTXF1), ==, 0x5566e5f6);
}

static uint8_t queue_pattern(unsigned packet, unsigned byte)
{
    return (uint8_t)(packet * 37 + byte);
}

static void fill_ep1_rx(unsigned packet_count, uint8_t packet_value)
{
    uint8_t packet[256];

    for (unsigned packet_index = 0; packet_index < packet_count;
         ++packet_index) {
        for (unsigned i = 0; i < sizeof(packet); ++i) {
            packet[i] = packet_value == 0xff
                ? queue_pattern(packet_index, i) : packet_value + i;
        }
        submit_packet(1, false, false, packet, sizeof(packet));
    }
}

static void assert_ep1_original_bytes(size_t start, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        size_t position = start + i;
        g_assert_cmpuint(qtest_readb(qts, USB_FIFO1), ==,
                         queue_pattern(position / 256, position % 256));
    }
}

static void test_ep1_fifo_boundaries(void)
{
    uint8_t overflow[256];

    reset_machine();
    fill_ep1_rx(16, 0xff);
    g_assert_cmpuint(qtest_readl(qts, TEST_EP1_RX_COUNT), ==, 4096);

    memset(overflow, 0xee, sizeof(overflow));
    submit_packet(1, false, false, overflow, sizeof(overflow));
    g_assert_cmpuint(qtest_readl(qts, TEST_EP1_RX_COUNT), ==, 4096);
    assert_ep1_original_bytes(0, 4096);
    g_assert_cmpuint(qtest_readl(qts, TEST_EP1_RX_COUNT), ==, 0);

    reset_machine();
    fill_ep1_rx(16, 0xff);
    assert_ep1_original_bytes(0, 128);
    g_assert_cmpuint(qtest_readl(qts, TEST_EP1_RX_COUNT), ==, 3968);

    fill_ep1_rx(1, 0xa5);
    g_assert_cmpuint(qtest_readl(qts, TEST_EP1_RX_COUNT), ==, 4096);
    assert_ep1_original_bytes(128, 3968);
    for (unsigned i = 0; i < 128; ++i) {
        g_assert_cmpuint(qtest_readb(qts, USB_FIFO1), ==, (uint8_t)(0xa5 + i));
    }
    g_assert_cmpuint(qtest_readl(qts, TEST_EP1_RX_COUNT), ==, 0);
}

static void test_device_mode(void)
{
    static const uint8_t get_device[] = {
        0x80, 6, 0, 1, 0, 0, sizeof(uint8_t) * 18, 0,
    };
    static const uint8_t get_config[] = { 0x80, 6, 0, 2, 0, 0, 0xff, 0 };
    static const uint8_t set_address[] = { 0, 5, 42, 0, 0, 0, 0, 0 };
    static const uint8_t set_config[] = { 0, 9, 1, 0, 0, 0, 0, 0 };
    uint8_t packet[128];
    size_t len;

    g_assert_cmpuint(qtest_readl(qts, USB_BASE + 0x40), ==, 0x4f54420a);
    g_assert_cmpuint(qtest_readl(qts, USB_DCFG) & USB_DCFG_DAD_MASK, ==, 0);

    submit_packet(0, false, true, get_device, sizeof(get_device));
    submit_packet(0, true, false, NULL, 0);
    len = receive_packet(packet, sizeof(packet));
    g_assert_cmpuint(len, ==, 18);
    g_assert_cmpuint(packet[0], ==, 18);
    g_assert_cmpuint(packet[1], ==, 1);
    g_assert_cmpuint(packet[8] | ((uint16_t)packet[9] << 8), ==, 0x0483);
    g_assert_cmpuint(packet[10] | ((uint16_t)packet[11] << 8), ==, 0x5741);

    submit_packet(0, false, true, get_config, sizeof(get_config));
    submit_packet(0, true, false, NULL, 0);
    len = receive_packet(packet, sizeof(packet));
    g_assert_cmpuint(len, ==, 64);
    g_assert_cmpuint(packet[1], ==, 2);
    submit_packet(0, true, false, NULL, 0);
    len = receive_packet(packet, sizeof(packet));
    g_assert_cmpuint(len, ==, 34);
    g_assert_cmpuint(packet[0], ==, 2);
    g_assert_cmpuint(packet[1], ==, 64);
    g_assert_cmpuint(packet[2], ==, 0);

    submit_packet(0, false, true, set_address, sizeof(set_address));
    submit_packet(0, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_DCFG) & USB_DCFG_DAD_MASK, ==,
                     42u << USB_DCFG_DAD_SHIFT);

    submit_packet(0, false, true, set_config, sizeof(set_config));
    submit_packet(0, true, false, NULL, 0);
    receive_packet(packet, sizeof(packet));
    g_assert_cmpuint(qtest_readl(qts, TEST_STATUS) & 0xff00, ==, 0x0100);

    submit_packet(1, false, false, (const uint8_t *)"OUT!", 4);
    g_assert_cmpuint(qtest_readl(qts, USB_DOEPINT0 + 0x20) & 1, ==, 1);
    /* EP1 OUT must propagate through DAINTMSK to the device-level
     * OEPINT summary (GINTSTS bit 19), as a real DWC2 guest driver uses
     * this summary before inspecting DOEPINT1. */
    g_assert_cmpuint(qtest_readl(qts, USB_GINTSTS) & (1u << 19), !=, 0);
    for (len = 0; len < 4; ++len) {
        g_assert_cmpuint(qtest_readb(qts, USB_FIFO1), ==, "OUT!"[len]);
    }

    for (len = 0; len < 5; ++len) {
        qtest_writeb(qts, USB_FIFO1, "IN OK"[len]);
    }
    submit_packet(1, true, false, NULL, 0);
    len = receive_packet(packet, sizeof(packet));
    g_assert_cmpuint(len, ==, 5);
    g_assert_cmpmem(packet, len, "IN OK", 5);
    g_assert_cmpuint(qtest_readl(qts, USB_DAINT) & (1u << 1), !=, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_DIEPINT0 + 0x20) & 1, ==, 1);
}

static void test_cdc_line_coding_control_transfers(void)
{
    static const uint8_t get_line_coding[] = {
        0xa1, 0x21, 0, 0, 0, 0, 7, 0,
    };
    static const uint8_t set_line_coding[] = {
        0x21, 0x20, 0, 0, 0, 0, 7, 0,
    };
    static const uint8_t set_control_line_state[] = {
        0x21, 0x22, 0x34, 0x12, 0, 0, 0, 0,
    };
    static const uint8_t line_coding[] = {
        0x00, 0x96, 0x00, 0x00, 0x00, 0x00, 0x08,
    };
    static const uint8_t default_line_coding[] = {
        0x00, 0xc2, 0x01, 0x00, 0x00, 0x00, 0x08,
    };
    uint8_t packet[64];

    reset_machine();
    submit_packet(0, false, true, get_line_coding,
                  sizeof(get_line_coding));
    submit_packet(0, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 7);
    g_assert_cmpmem(packet, 7, default_line_coding,
                    sizeof(default_line_coding));
    submit_packet(0, false, false, NULL, 0);

    submit_packet(0, false, true, set_line_coding,
                  sizeof(set_line_coding));
    submit_packet(0, false, false, line_coding, 3);
    submit_packet(0, false, false, line_coding + 3, 4);
    submit_packet(0, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 0);

    submit_packet(0, false, true, get_line_coding,
                  sizeof(get_line_coding));
    submit_packet(0, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 7);
    g_assert_cmpmem(packet, 7, line_coding, sizeof(line_coding));
    submit_packet(0, false, false, NULL, 0);

    submit_packet(0, false, true, set_control_line_state,
                  sizeof(set_control_line_state));
    submit_packet(0, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 0);
}

static void test_control_stall_is_observable(void)
{
    static const uint8_t unsupported[] = {
        0x80, 0xff, 0, 0, 0, 0, 0, 0,
    };

    reset_machine();
    submit_packet(0, false, true, unsupported, sizeof(unsupported));
    g_assert_cmpuint(qtest_readl(qts, TEST_STATUS) & TEST_EP0_STALLED, !=, 0);
}

static void test_empty_in_is_nak(void)
{
    reset_machine();
    submit_packet(1, true, false, NULL, 0);
    g_assert_cmpuint(qtest_readl(qts, TEST_STATUS) & (1u << 17), ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_DAINT) & (1u << 1), ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_DIEPINT0 + 0x20) & 1, ==, 0);
}

static void test_advertised_endpoint_queues(void)
{
    static const struct {
        unsigned ep;
        uint64_t fifo;
        uint64_t diepint;
        uint64_t doepint;
    } endpoints[] = {
        { 2, USB_FIFO2, USB_DIEPINT0 + 2 * 0x20,
          USB_DOEPINT0 + 2 * 0x20 },
        { 3, USB_FIFO3, USB_DIEPINT0 + 3 * 0x20,
          USB_DOEPINT0 + 3 * 0x20 },
        { 4, USB_FIFO4, USB_DIEPINT0 + 4 * 0x20,
          USB_DOEPINT0 + 4 * 0x20 },
        { 5, USB_FIFO5, USB_DIEPINT0 + 5 * 0x20,
          USB_DOEPINT0 + 5 * 0x20 },
    };

    reset_machine();
    for (unsigned i = 0; i < ARRAY_SIZE(endpoints); ++i) {
        const char out_byte = (char)('A' + endpoints[i].ep);
        uint8_t packet[8];
        size_t len;

        if (endpoints[i].ep == 2 || endpoints[i].ep == 5) {
            submit_packet(endpoints[i].ep, false, false,
                          (const uint8_t *)&out_byte, 1);
            g_assert_cmpuint(qtest_readl(qts, endpoints[i].doepint) & 1,
                             ==, 1);
            g_assert_cmpuint(qtest_readl(qts, USB_GINTSTS) & (1u << 19),
                             !=, 0);
            g_assert_cmpuint(qtest_readb(qts, endpoints[i].fifo), ==,
                             (uint8_t)out_byte);
        } else {
            qtest_writeb(qts, endpoints[i].fifo, out_byte);
            submit_packet(endpoints[i].ep, true, false, NULL, 0);
            len = receive_packet(packet, sizeof(packet));
            g_assert_cmpuint(len, ==, 1);
            g_assert_cmpuint(packet[0], ==, (uint8_t)out_byte);
            g_assert_cmpuint(qtest_readl(qts, endpoints[i].diepint) & 1,
                             ==, 1);
        }
    }
}

static void test_endpoint_packet_boundaries(void)
{
    uint8_t tx[130];
    uint8_t packet[128];

    reset_machine();
    for (unsigned i = 0; i < sizeof(tx); ++i) {
        tx[i] = (uint8_t)(i ^ 0x5a);
        qtest_writeb(qts, USB_FIFO1, tx[i]);
    }

    submit_packet(1, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 64);
    g_assert_cmpmem(packet, 64, tx, 64);

    submit_packet(1, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 64);
    g_assert_cmpmem(packet, 64, tx + 64, 64);

    submit_packet(1, true, false, NULL, 0);
    g_assert_cmpuint(receive_packet(packet, sizeof(packet)), ==, 2);
    g_assert_cmpmem(packet, 2, tx + 128, 2);
}

static void test_irq_reaches_nvic(void)
{
    static const uint8_t byte = 0x5c;

    reset_machine();
    qtest_writel(qts, USB_GAHBCFG, 1);
    qtest_writel(qts, USB_GINTMSK, USB_GINT_OEPINT | USB_GINT_IEPINT);
    submit_packet(1, false, false, &byte, 1);
    g_assert_cmpuint(qtest_readl(qts, USB_GINTSTS) & USB_GINT_OEPINT,
                     !=, 0);
    g_assert_cmpuint(qtest_readl(qts, NVIC_ISPR2) & USB_IRQ_BIT, !=, 0);

    qtest_writel(qts, USB_DOEPINT0 + 0x20, 1);
    g_assert_cmpuint(qtest_readl(qts, USB_GINTSTS) & USB_GINT_OEPINT,
                     ==, 0);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
    g_assert_cmpuint(qtest_readl(qts, NVIC_ISPR2) & USB_IRQ_BIT, ==, 0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    qts = qtest_init("-machine dm-mc02");
    g_test_add_func("/dm-mc02/usb/device-mode-cdc", test_device_mode);
    g_test_add_func("/dm-mc02/usb/cdc-line-coding",
                    test_cdc_line_coding_control_transfers);
    g_test_add_func("/dm-mc02/usb/control-stall",
                    test_control_stall_is_observable);
    g_test_add_func("/dm-mc02/usb/empty-in-nak", test_empty_in_is_nak);
    g_test_add_func("/dm-mc02/usb/descriptor-contract", test_descriptor_contract);
    g_test_add_func("/dm-mc02/usb/power-on-tx-fifo-registers",
                    test_power_on_tx_fifo_registers);
    g_test_add_func("/dm-mc02/usb/ep1-fifo-boundaries", test_ep1_fifo_boundaries);
    g_test_add_func("/dm-mc02/usb/advertised-endpoints",
                    test_advertised_endpoint_queues);
    g_test_add_func("/dm-mc02/usb/packet-boundaries",
                    test_endpoint_packet_boundaries);
    g_test_add_func("/dm-mc02/usb/irq-reaches-nvic",
                    test_irq_reaches_nvic);
    int ret = g_test_run();
    qtest_quit(qts);
    return ret;
}
