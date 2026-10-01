/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"

#define USB_BASE       0x40040000
#define USB_GAHBCFG    (USB_BASE + 0x008)
#define USB_GINTSTS    (USB_BASE + 0x014)
#define USB_GINTMSK    (USB_BASE + 0x018)
#define USB_HFNUM      (USB_BASE + 0x408)
#define USB_HAINT      (USB_BASE + 0x414)
#define USB_HAINTMSK   (USB_BASE + 0x418)
#define USB_HPRT0      (USB_BASE + 0x440)
#define USB_HCCHAR0    (USB_BASE + 0x500)
#define USB_HCINT0     (USB_BASE + 0x508)
#define USB_HCINTMSK0  (USB_BASE + 0x50c)
#define USB_HCTSIZ0    (USB_BASE + 0x510)
#define USB_HCFIFO0    (USB_BASE + 0x1000)
#define USB_HCCHAR(channel) (USB_BASE + 0x500 + 0x20 * (channel))
#define USB_HCINT(channel) (USB_BASE + 0x508 + 0x20 * (channel))
#define USB_HCINTMSK(channel) (USB_BASE + 0x50c + 0x20 * (channel))
#define USB_HCTSIZ(channel) (USB_BASE + 0x510 + 0x20 * (channel))
#define USB_HCFIFO(channel) (USB_BASE + 0x1000 + 0x1000 * (channel))

#define USB_GAHBCFG_GINT       (1u << 0)
#define USB_GINTSTS_SOF        (1u << 3)
#define USB_GINTSTS_PRTINT     (1u << 24)
#define USB_GINTSTS_HCINT      (1u << 25)
#define USB_HPRT0_PWR          (1u << 12)
#define USB_HPRT0_RST          (1u << 8)
#define USB_HPRT0_ENACHG       (1u << 3)
#define USB_HPRT0_ENA          (1u << 2)
#define USB_HPRT0_CONNDET      (1u << 1)
#define USB_HPRT0_CONNSTS      (1u << 0)
#define USB_HPRT0_SPD_SHIFT    17
#define USB_HPRT0_SPD_MASK     (3u << USB_HPRT0_SPD_SHIFT)
#define USB_HPRT0_SPD_HIGH     0u
#define USB_HCCHAR_CHENA       (1u << 31)
#define USB_HCCHAR_CHDIS       (1u << 30)
#define USB_HCCHAR_EPDIR       (1u << 15)
#define USB_HCCHAR_MPS         64u
#define USB_HCINT_XFRC         (1u << 0)
#define USB_HCINT_CHHLTD       (1u << 1)
#define USB_HCTSIZ_PID_SHIFT   29
#define USB_HCTSIZ_PKT_SHIFT   19
#define USB_HCTSIZ_PID_DATA1   2u
#define USB_HCTSIZ_PID_SETUP   3u
#define NVIC_ISPR2             0xe000e208
#define NVIC_ICPR2             0xe000e288
#define USB_IRQ_BIT            (1u << 13)

static QTestState *qts;

static void reset_machine(void)
{
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
}

static void enable_port(void)
{
    uint32_t hprt;

    hprt = qtest_readl(qts, USB_HPRT0);
    g_assert_true(hprt & USB_HPRT0_PWR);
    g_assert_true(hprt & USB_HPRT0_CONNSTS);
    g_assert_true(hprt & USB_HPRT0_CONNDET);

    qtest_writel(qts, USB_GAHBCFG, USB_GAHBCFG_GINT);
    qtest_writel(qts, USB_GINTMSK, USB_GINTSTS_PRTINT | USB_GINTSTS_HCINT |
                 USB_GINTSTS_SOF);
    g_assert_true(qtest_readl(qts, NVIC_ISPR2) & USB_IRQ_BIT);
    qtest_writel(qts, USB_HPRT0, USB_HPRT0_PWR | USB_HPRT0_CONNDET |
                 USB_HPRT0_RST);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
    qtest_writel(qts, USB_HPRT0, USB_HPRT0_PWR);
    hprt = qtest_readl(qts, USB_HPRT0);
    g_assert_true(hprt & USB_HPRT0_CONNSTS);
    g_assert_true(hprt & USB_HPRT0_ENA);
    g_assert_true(hprt & USB_HPRT0_ENACHG);
}

static void test_port_irq_and_sof(void)
{
    uint32_t frame_before;
    uint32_t hprt;
    uint64_t frame_period_ns;

    reset_machine();
    enable_port();
    qtest_writel(qts, USB_HPRT0, USB_HPRT0_PWR | USB_HPRT0_ENACHG);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
    hprt = qtest_readl(qts, USB_HPRT0);
    frame_period_ns = ((hprt & USB_HPRT0_SPD_MASK) >> USB_HPRT0_SPD_SHIFT) ==
                      USB_HPRT0_SPD_HIGH ? 125000 : 1000000;
    frame_before = qtest_readl(qts, USB_HFNUM);
    qtest_clock_step(qts, frame_period_ns);
    g_assert_cmpuint(qtest_readl(qts, USB_HFNUM), ==, frame_before + 1);
    g_assert_true(qtest_readl(qts, USB_GINTSTS) & USB_GINTSTS_SOF);
    g_assert_true(qtest_readl(qts, NVIC_ISPR2) & USB_IRQ_BIT);
    qtest_writel(qts, USB_GINTSTS, USB_GINTSTS_SOF);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
}

static void write_setup(unsigned channel, const uint8_t setup[8])
{
    for (unsigned i = 0; i < 8; i += 4) {
        uint32_t word = setup[i] | ((uint32_t)setup[i + 1] << 8) |
                        ((uint32_t)setup[i + 2] << 16) |
                        ((uint32_t)setup[i + 3] << 24);

        qtest_writel(qts, USB_HCFIFO(channel), word);
    }
}

static void start_setup_channel(unsigned channel, const uint8_t setup[8])
{
    write_setup(channel, setup);
    qtest_writel(qts, USB_HCINTMSK(channel),
                 USB_HCINT_XFRC | USB_HCINT_CHHLTD);
    qtest_writel(qts, USB_HCTSIZ(channel),
                 8 | (1u << USB_HCTSIZ_PKT_SHIFT) |
                 (USB_HCTSIZ_PID_SETUP << USB_HCTSIZ_PID_SHIFT));
    qtest_writel(qts, USB_HCCHAR(channel), USB_HCCHAR_MPS |
                 USB_HCCHAR_CHENA);
}

static void test_reset_cancels_deferred_completion(void)
{
    static const uint8_t get_descriptor[] = {
        0x80, 0x06, 0, 0x01, 0, 0, 18, 0,
    };

    reset_machine();
    enable_port();
    qtest_writel(qts, USB_HAINTMSK, 1);
    qtest_writel(qts, USB_HCINTMSK0, USB_HCINT_XFRC | USB_HCINT_CHHLTD);
    qtest_writel(qts, USB_HPRT0, USB_HPRT0_PWR | USB_HPRT0_ENACHG);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
    write_setup(0, get_descriptor);
    qtest_writel(qts, USB_HCTSIZ0,
                 8 | (1u << USB_HCTSIZ_PKT_SHIFT) |
                 (USB_HCTSIZ_PID_SETUP << USB_HCTSIZ_PID_SHIFT));
    qtest_writel(qts, USB_HCCHAR0, USB_HCCHAR_MPS | USB_HCCHAR_CHENA);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT0), ==, 0);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_clock_step(qts, 1000);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT0), ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_HAINTMSK), ==, 0);
}

static void test_control_transfer_to_qemu_device(void)
{
    static const uint8_t get_descriptor[] = {
        0x80, 0x06, 0, 0x01, 0, 0, 18, 0,
    };
    uint8_t descriptor[18];
    uint32_t hcint;

    reset_machine();
    enable_port();
    qtest_writel(qts, USB_HAINTMSK, 1);
    qtest_writel(qts, USB_HCINTMSK0, USB_HCINT_XFRC | USB_HCINT_CHHLTD);
    qtest_writel(qts, USB_HPRT0, USB_HPRT0_PWR | USB_HPRT0_ENACHG);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);

    write_setup(0, get_descriptor);
    qtest_writel(qts, USB_HCTSIZ0,
                 8 | (1u << USB_HCTSIZ_PKT_SHIFT) |
                 (USB_HCTSIZ_PID_SETUP << USB_HCTSIZ_PID_SHIFT));
    qtest_writel(qts, USB_HCCHAR0, USB_HCCHAR_MPS | USB_HCCHAR_CHENA);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT0), ==, 0);
    qtest_clock_step(qts, 999);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT0), ==, 0);
    qtest_clock_step(qts, 1);
    hcint = qtest_readl(qts, USB_HCINT0);
    g_assert_cmpuint(hcint, ==, USB_HCINT_XFRC | USB_HCINT_CHHLTD);
    g_assert_true(qtest_readl(qts, USB_GINTSTS) & USB_GINTSTS_HCINT);
    g_assert_true(qtest_readl(qts, NVIC_ISPR2) & USB_IRQ_BIT);
    qtest_writel(qts, USB_HCINT0, hcint);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);

    qtest_writel(qts, USB_HCTSIZ0,
                 18 | (1u << USB_HCTSIZ_PKT_SHIFT) |
                 (USB_HCTSIZ_PID_DATA1 << USB_HCTSIZ_PID_SHIFT));
    qtest_writel(qts, USB_HCCHAR0,
                 USB_HCCHAR_MPS | USB_HCCHAR_EPDIR | USB_HCCHAR_CHENA);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT0), ==, 0);
    qtest_clock_step(qts, 1000);
    hcint = qtest_readl(qts, USB_HCINT0);
    g_assert_cmpuint(hcint, ==, USB_HCINT_XFRC | USB_HCINT_CHHLTD);
    for (unsigned i = 0; i < sizeof(descriptor); ++i) {
        descriptor[i] = qtest_readb(qts, USB_HCFIFO0);
    }
    g_assert_cmpuint(descriptor[0], ==, 18);
    g_assert_cmpuint(descriptor[1], ==, 1);
    g_assert_cmpuint(descriptor[7], ==, 64);
    g_assert_cmpuint(descriptor[8] | ((uint16_t)descriptor[9] << 8), ==,
                     0x0627);
    g_assert_cmpuint(descriptor[10] | ((uint16_t)descriptor[11] << 8), ==,
                     1);
    qtest_writel(qts, USB_HCINT0, hcint);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
}

static void test_multiple_deferred_channels_and_cancel(void)
{
    static const uint8_t get_descriptor[] = {
        0x80, 0x06, 0, 0x01, 0, 0, 18, 0,
    };
    uint32_t hcint;

    reset_machine();
    enable_port();
    qtest_writel(qts, USB_HAINTMSK, (1u << 0) | (1u << 1));
    qtest_writel(qts, USB_HPRT0, USB_HPRT0_PWR | USB_HPRT0_ENACHG);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
    start_setup_channel(0, get_descriptor);
    start_setup_channel(1, get_descriptor);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(0)), ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(1)), ==, 0);
    qtest_clock_step(qts, 999);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(0)), ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(1)), ==, 0);
    qtest_clock_step(qts, 1);
    hcint = USB_HCINT_XFRC | USB_HCINT_CHHLTD;
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(0)), ==, hcint);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(1)), ==, hcint);
    g_assert_cmpuint(qtest_readl(qts, USB_HAINT), ==, 3);
    g_assert_true(qtest_readl(qts, USB_GINTSTS) & USB_GINTSTS_HCINT);
    g_assert_true(qtest_readl(qts, NVIC_ISPR2) & USB_IRQ_BIT);
    qtest_writel(qts, USB_HCINT(0), hcint);
    qtest_writel(qts, USB_HCINT(1), hcint);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);

    reset_machine();
    enable_port();
    qtest_writel(qts, USB_HAINTMSK, (1u << 0) | (1u << 1));
    qtest_writel(qts, USB_HPRT0, USB_HPRT0_PWR | USB_HPRT0_ENACHG);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
    start_setup_channel(0, get_descriptor);
    start_setup_channel(1, get_descriptor);
    qtest_writel(qts, USB_HCCHAR(0), USB_HCCHAR_CHDIS);
    g_assert_cmpuint(qtest_readl(qts, USB_HCCHAR(0)) & USB_HCCHAR_CHENA,
                     ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(0)), ==,
                     USB_HCINT_CHHLTD);
    g_assert_cmpuint(qtest_readl(qts, USB_HAINT), ==, 1);
    qtest_writel(qts, USB_HCINT(0), USB_HCINT_CHHLTD);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
    qtest_clock_step(qts, 999);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(1)), ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(0)), ==, 0);
    g_assert_cmpuint(qtest_readl(qts, USB_HCINT(1)), ==, hcint);
    g_assert_cmpuint(qtest_readl(qts, USB_HAINT), ==, 2);
    g_assert_true(qtest_readl(qts, USB_GINTSTS) & USB_GINTSTS_HCINT);
    g_assert_true(qtest_readl(qts, NVIC_ISPR2) & USB_IRQ_BIT);
    qtest_writel(qts, USB_HCINT(1), hcint);
    qtest_writel(qts, NVIC_ICPR2, USB_IRQ_BIT);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    qts = qtest_init("-machine stm32h723-usb-host "
                     "-global dm-stm32h7-otg-host-qemu.completion-delay-ns=1000 "
                     "-device usb-kbd,bus=usb-bus.0,port=1");

    qtest_add_func("/stm32h723-usb-host/port-irq-sof", test_port_irq_and_sof);
    qtest_add_func("/stm32h723-usb-host/reset-cancels-completion",
                   test_reset_cancels_deferred_completion);
    qtest_add_func("/stm32h723-usb-host/control-transfer",
                   test_control_transfer_to_qemu_device);
    qtest_add_func("/stm32h723-usb-host/multiple-deferred-channels",
                   test_multiple_deferred_channels_and_cancel);
    int result = g_test_run();

    qtest_quit(qts);
    return result;
}
