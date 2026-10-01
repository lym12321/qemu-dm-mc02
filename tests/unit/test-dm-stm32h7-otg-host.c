/* Tests for the reusable STM32H7 DWC2 host-port register subset. */
#include "qemu/osdep.h"
#include "hw/usb/dm_stm32h7_otg_host.h"
#include "hw/usb/dm_usb_control.h"
#include "hw/usb/dm_usb_dwc2_device.h"

typedef struct TestOtgHost {
    DmStm32H7OtgHost host;
    unsigned reset_calls;
    uint64_t reset_timestamp_ns;
    bool irq_level;
    unsigned irq_changes;
    unsigned channel_start_calls;
    unsigned channel_cancel_calls;
    uint8_t canceled_channel;
    uint64_t canceled_token;
    DmStm32H7OtgHostChannelRequest request;
} TestOtgHost;

static void test_port_reset(void *opaque, uint64_t timestamp_ns)
{
    TestOtgHost *test = opaque;

    ++test->reset_calls;
    test->reset_timestamp_ns = timestamp_ns;
}

static void test_irq(void *opaque, bool level)
{
    TestOtgHost *test = opaque;

    test->irq_level = level;
    ++test->irq_changes;
}

static void test_channel_start(
    void *opaque, const DmStm32H7OtgHostChannelRequest *request)
{
    TestOtgHost *test = opaque;

    ++test->channel_start_calls;
    test->request = *request;
}

static void test_channel_cancel(void *opaque, DmStm32H7OtgHost *host,
                                unsigned channel, uint64_t completion_token)
{
    TestOtgHost *test = opaque;

    (void)host;
    ++test->channel_cancel_calls;
    test->canceled_channel = channel;
    test->canceled_token = completion_token;
}

static void test_init(TestOtgHost *test)
{
    memset(test, 0, sizeof(*test));
    dm_stm32h7_otg_host_init(&test->host, test_port_reset, test,
                              test_irq, test);
    dm_stm32h7_otg_host_set_channel_start(&test->host, test_channel_start,
                                           test);
}

static void test_enable_port(TestOtgHost *test)
{
    dm_stm32h7_otg_host_set_port_connected(&test->host, true,
                                            DM_STM32H7_OTG_PORT_FULL_SPEED);
    dm_stm32h7_otg_host_write(&test->host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 50);
    dm_stm32h7_otg_host_write(&test->host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 60);
}

static void test_reset_defaults(void)
{
    TestOtgHost test;

    test_init(&test);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_GAHBCFG), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_GINTSTS), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCFG), ==,
                     DM_STM32H7_OTG_HCFG_RESVALID);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFIR), ==,
                     DM_STM32H7_OTG_HFIR_RESET);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HPRT0), ==,
                     DM_STM32H7_OTG_HPRT0_PWR);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCCHAR(0)), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==, 0);
    g_assert_false(test.irq_level);
}

static void test_channel_issue_complete_and_irq(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT) |
                       (DM_STM32H7_OTG_HOST_PID_DATA1 <<
                        DM_STM32H7_OTG_HCTSIZ_PID_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA |
                      (5u << DM_STM32H7_OTG_HCCHAR_DEVADDR_SHIFT) |
                      (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      DM_STM32H7_OTG_HCCHAR_EPDIR |
                      (3u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTMSK,
                               DM_STM32H7_OTG_GINTSTS_HCINT, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HAINTMSK, 1, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINTMSK(0),
                               DM_STM32H7_OTG_HCINT_XFRC |
                               DM_STM32H7_OTG_HCINT_CHHLTD, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_GINT, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 123456);

    g_assert_cmpuint(test.channel_start_calls, ==, 1);
    g_assert_cmpuint(test.request.channel, ==, 0);
    g_assert_cmpuint(test.request.device_address, ==, 5);
    g_assert_cmpuint(test.request.endpoint, ==, 3);
    g_assert_cmpuint(test.request.endpoint_type, ==, 2);
    g_assert_true(test.request.direction_in);
    g_assert_cmpuint(test.request.max_packet_size, ==, 64);
    g_assert_cmpuint(test.request.transfer_size, ==, 64);
    g_assert_cmpuint(test.request.packet_count, ==, 1);
    g_assert_cmpint(test.request.pid, ==, DM_STM32H7_OTG_HOST_PID_DATA1);
    g_assert_cmpuint(test.request.timestamp_ns, ==, 123456);

    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCCHAR(0)) &
                     DM_STM32H7_OTG_HCCHAR_CHENA, ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)) &
                     (DM_STM32H7_OTG_HCTSIZ_XFERSIZE_MASK |
                      DM_STM32H7_OTG_HCTSIZ_PKTCNT_MASK), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==, 1);
    g_assert_true(dm_stm32h7_otg_host_read(&test.host,
                                            DM_STM32H7_OTG_GINTSTS) &
                  DM_STM32H7_OTG_GINTSTS_HCINT);
    g_assert_true(test.irq_level);

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINT(0),
                               DM_STM32H7_OTG_HCINT_XFRC, 0);
    g_assert_true(test.irq_level);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINT(0),
                               DM_STM32H7_OTG_HCINT_CHHLTD, 0);
    g_assert_false(test.irq_level);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==, 0);
}

static void test_deferred_multi_channel_irq_masks(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 8 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar1 = DM_STM32H7_OTG_HCCHAR_CHENA |
                       (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) | 64;
    uint32_t hcchar3 = hcchar1 |
                       (3u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT);
    uint32_t channel_mask = (1u << 1) | (1u << 3);
    uint32_t completion_mask = DM_STM32H7_OTG_HCINT_XFRC |
                               DM_STM32H7_OTG_HCINT_CHHLTD;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTMSK,
                               DM_STM32H7_OTG_GINTSTS_HCINT, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HAINTMSK,
                               channel_mask, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINTMSK(1),
                               completion_mask, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINTMSK(3), 0, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_GINT, 0);

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(1),
                               hctsiz, 100);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(1),
                               hcchar1, 100);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(3),
                               hctsiz, 110);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(3),
                               hcchar3, 110);

    g_assert_cmpuint(test.channel_start_calls, ==, 2);
    g_assert_true(test.host.channel[1].waiting_completion);
    g_assert_true(test.host.channel[3].waiting_completion);
    g_assert_false(test.irq_level);

    /* Completion is deliberately supplied later than channel_start(). */
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 1, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 8);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==,
                     1u << 1);
    g_assert_true(dm_stm32h7_otg_host_read(
                      &test.host, DM_STM32H7_OTG_GINTSTS) &
                  DM_STM32H7_OTG_GINTSTS_HCINT);
    g_assert_true(test.irq_level);

    /* HCINTMSK(3) keeps a real channel completion out of HAINT. */
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 3, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 8);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==,
                     1u << 1);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINTMSK(3),
                               completion_mask, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==,
                     channel_mask);

    /* HAINTMSK(1) suppresses channel 1 while channel 3 keeps IRQ asserted. */
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HAINTMSK,
                               1u << 3, 0);
    g_assert_true(test.irq_level);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINT(3),
                               completion_mask, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==,
                     1u << 1);
    g_assert_false(test.irq_level);

    /* Restoring HAINTMSK exposes the still-pending channel 1 completion. */
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HAINTMSK,
                               channel_mask, 0);
    g_assert_true(test.irq_level);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTMSK, 0, 0);
    g_assert_false(test.irq_level);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTSTS,
                               DM_STM32H7_OTG_GINTSTS_HCINT, 0);
    g_assert_true(dm_stm32h7_otg_host_read(
                      &test.host, DM_STM32H7_OTG_GINTSTS) &
                  DM_STM32H7_OTG_GINTSTS_HCINT);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTMSK,
                               DM_STM32H7_OTG_GINTSTS_HCINT, 0);
    g_assert_true(test.irq_level);

    /* Both HCINT bits must be W1C-cleared before the level IRQ drops. */
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINT(1),
                               DM_STM32H7_OTG_HCINT_XFRC, 0);
    g_assert_true(test.irq_level);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCINT(1),
                               DM_STM32H7_OTG_HCINT_CHHLTD, 0);
    g_assert_false(test.irq_level);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HAINT), ==, 0);
}

static void test_channel_nak_is_serviceable(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 128 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA |
                      (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_NAK, 0);
    g_assert_true(dm_stm32h7_otg_host_read(
                          &test.host, DM_STM32H7_OTG_HCCHAR(0)) &
                  DM_STM32H7_OTG_HCCHAR_CHENA);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==, hctsiz);
    g_assert_true(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCINT(0)) &
                  DM_STM32H7_OTG_HCINT_NAK);
    g_assert_false(dm_stm32h7_otg_host_read(
                          &test.host, DM_STM32H7_OTG_HCINT(0)) &
                   DM_STM32H7_OTG_HCINT_CHHLTD);
    g_assert_true(dm_stm32h7_otg_host_service_channel(&test.host, 0, 200));
    g_assert_cmpuint(test.channel_start_calls, ==, 2);
    g_assert_cmpuint(test.request.timestamp_ns, ==, 200);
}

static void test_channel_multi_packet_requires_service(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 100 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);
    g_assert_cmpuint(test.request.transfer_size, ==, 64);
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
    g_assert_true(dm_stm32h7_otg_host_read(
                          &test.host, DM_STM32H7_OTG_HCCHAR(0)) &
                  DM_STM32H7_OTG_HCCHAR_CHENA);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                     36 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCINT(0)), ==, 0);
    g_assert_true(dm_stm32h7_otg_host_service_channel(&test.host, 0, 200));
    g_assert_cmpuint(test.channel_start_calls, ==, 2);
    g_assert_cmpuint(test.request.transfer_size, ==, 36);
    g_assert_cmpint(test.request.pid, ==, DM_STM32H7_OTG_HOST_PID_DATA1);
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 36);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCCHAR(0)) &
                     DM_STM32H7_OTG_HCCHAR_CHENA, ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_channel_multi_packet_short_stops(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 128 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);

    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 8);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                     120 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCCHAR(0)) &
                     DM_STM32H7_OTG_HCCHAR_CHENA, ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
    g_assert_false(dm_stm32h7_otg_host_service_channel(&test.host, 0, 200));
    g_assert_cmpuint(test.channel_start_calls, ==, 1);
}

static void test_channel_zero_length_completion_preserves_packet_count(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 128 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);

    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==, hctsiz);
}

static void test_channel_short_completion_consumes_one_packet(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 192 | (3u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);

    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 8);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                     184 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
}

static void test_disconnect_cancels_pending_completion(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT) |
                      (DM_STM32H7_OTG_HOST_PID_DATA1 <<
                       DM_STM32H7_OTG_HCTSIZ_PID_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;
    uint32_t expected_hcdma = 0x24000000;
    uint32_t expected_hcint;
    uint32_t expected_hctsiz;
    DmStm32H7OtgHostChannelPid expected_pid;
    uint64_t token;

    test_init(&test);
    dm_stm32h7_otg_host_set_channel_cancel(&test.host, test_channel_cancel,
                                            &test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCDMA(0),
                               expected_hcdma, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    token = test.request.completion_token;

    dm_stm32h7_otg_host_set_port_connected(&test.host, false,
                                            DM_STM32H7_OTG_PORT_FULL_SPEED);
    g_assert_cmpuint(test.channel_cancel_calls, ==, 1);
    g_assert_cmpuint(test.canceled_channel, ==, 0);
    g_assert_cmpuint(test.canceled_token, ==, token);
    expected_hctsiz = dm_stm32h7_otg_host_read(&test.host,
                                                DM_STM32H7_OTG_HCTSIZ(0));
    expected_pid = test.host.channel[0].next_pid;
    expected_hcint = dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCINT(0));
    dm_stm32h7_otg_host_complete_channel_with_token(
        &test.host, 0, token, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                     expected_hctsiz);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCDMA(0)), ==,
                     expected_hcdma);
    g_assert_cmpint(test.host.channel[0].next_pid, ==, expected_pid);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCINT(0)), ==,
                     expected_hcint);
}

static void test_port_power_off_cancels_pending_completion(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT) |
                      (DM_STM32H7_OTG_HOST_PID_DATA1 <<
                       DM_STM32H7_OTG_HCTSIZ_PID_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;
    uint32_t expected_hcdma = 0x24000100;
    uint32_t expected_hcint;
    uint32_t expected_hctsiz;
    DmStm32H7OtgHostChannelPid expected_pid;
    uint64_t token;

    test_init(&test);
    dm_stm32h7_otg_host_set_channel_cancel(&test.host, test_channel_cancel,
                                            &test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCDMA(0),
                               expected_hcdma, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    token = test.request.completion_token;

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0, 0, 200);
    g_assert_cmpuint(test.channel_cancel_calls, ==, 1);
    g_assert_cmpuint(test.canceled_channel, ==, 0);
    g_assert_cmpuint(test.canceled_token, ==, token);
    expected_hctsiz = dm_stm32h7_otg_host_read(&test.host,
                                                DM_STM32H7_OTG_HCTSIZ(0));
    expected_pid = test.host.channel[0].next_pid;
    expected_hcint = dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCINT(0));
    dm_stm32h7_otg_host_complete_channel_with_token(
        &test.host, 0, token, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                     expected_hctsiz);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCDMA(0)), ==,
                     expected_hcdma);
    g_assert_cmpint(test.host.channel[0].next_pid, ==, expected_pid);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCINT(0)), ==,
                     expected_hcint);
}

static void test_hcdma_progress_and_controller_reset(void)
{
    TestOtgHost test;
    uint32_t one_packet = 4 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t two_packets = 100 |
                           (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCDMA(0),
                               0x24000000, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               one_packet, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0), hcchar,
                               1);
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&test.host,
                                              DM_STM32H7_OTG_HCDMA(0)), ==,
                    0x24000000);

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCDMA(0),
                               0x24000100, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               two_packets, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0), hcchar,
                               2);
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&test.host,
                                              DM_STM32H7_OTG_HCDMA(0)), ==,
                    0x24000140);
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_NAK, 0);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&test.host,
                                              DM_STM32H7_OTG_HCDMA(0)), ==,
                    0x24000140);
    g_assert_true(dm_stm32h7_otg_host_service_channel(&test.host, 0, 3));
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 36);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&test.host,
                                              DM_STM32H7_OTG_HCDMA(0)), ==,
                    0x24000164);

    dm_stm32h7_otg_host_reset(&test.host);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCCHAR(0)), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCTSIZ(0)), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HCDMA(0)), ==, 0);
}

static void test_sof_frame_and_channel_schedule(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 128 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTMSK,
                               DM_STM32H7_OTG_GINTSTS_SOF, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_GINT, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 70);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);
    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);

    dm_stm32h7_otg_host_advance_time(&test.host, 1000059);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFNUM), ==, 0);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);
    dm_stm32h7_otg_host_advance_time(&test.host, 1000060);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFNUM), ==, 1);
    g_assert_cmpuint(test.channel_start_calls, ==, 2);
    g_assert_cmpuint(test.request.transfer_size, ==, 64);
    g_assert_cmpuint(test.request.timestamp_ns, ==, 1000060);
    g_assert_true(dm_stm32h7_otg_host_read(&test.host,
                                            DM_STM32H7_OTG_GINTSTS) &
                  DM_STM32H7_OTG_GINTSTS_SOF);
    g_assert_true(test.irq_level);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTSTS,
                               DM_STM32H7_OTG_GINTSTS_SOF, 0);
    g_assert_false(test.irq_level);
}

static void test_periodic_sof_frame_parity(void)
{
    const bool odd_frame[] = { true, false };

    for (unsigned mode = 0; mode < ARRAY_SIZE(odd_frame); ++mode) {
        TestOtgHost test;
        DmStm32H7OtgHostChannelPid first_pid;
        DmStm32H7OtgHostChannelPid second_pid;
        uint32_t hctsiz = 192 | (3u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
        uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA |
                          (3u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) | 64;
        uint32_t initial_hctsiz;

        if (odd_frame[mode]) {
            hcchar |= DM_STM32H7_OTG_HCCHAR_ODDFRM;
        }

        test_init(&test);
        test_enable_port(&test);
        dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                                   hctsiz, 0);
        dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                                   hcchar, 100);
        g_assert_cmpuint(test.channel_start_calls, ==, 1);
        first_pid = test.request.pid;
        dm_stm32h7_otg_host_complete_channel(
            &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
        second_pid = test.host.channel[0].next_pid;
        g_assert_cmpint(second_pid, !=, first_pid);
        initial_hctsiz = dm_stm32h7_otg_host_read(
            &test.host, DM_STM32H7_OTG_HCTSIZ(0));
        g_assert_cmpuint(initial_hctsiz, ==,
                         128 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));

        dm_stm32h7_otg_host_advance_time(&test.host, 1000059);
        g_assert_cmpuint(test.channel_start_calls, ==, 1);
        g_assert_cmpuint(dm_stm32h7_otg_host_read(
                             &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                         initial_hctsiz);

        dm_stm32h7_otg_host_advance_time(&test.host, 1000060);
        g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                                   DM_STM32H7_OTG_HFNUM), ==,
                         1);
        g_assert_cmpuint(test.channel_start_calls, ==,
                         odd_frame[mode] ? 2 : 1);
        g_assert_cmpuint(dm_stm32h7_otg_host_read(
                             &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                         initial_hctsiz);

        if (odd_frame[mode]) {
            g_assert_cmpint(test.request.pid, ==, second_pid);
            dm_stm32h7_otg_host_complete_channel(
                &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
            g_assert_cmpuint(dm_stm32h7_otg_host_read(
                                 &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                             64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
        } else {
        }

        dm_stm32h7_otg_host_advance_time(&test.host, 2000060);
        g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                                   DM_STM32H7_OTG_HFNUM), ==,
                         2);
        g_assert_cmpuint(test.channel_start_calls, ==, 2);
        if (!odd_frame[mode]) {
            g_assert_cmpint(test.request.pid, ==, second_pid);
            dm_stm32h7_otg_host_complete_channel(
                &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 64);
            g_assert_cmpuint(dm_stm32h7_otg_host_read(
                                 &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                             64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
        }

        dm_stm32h7_otg_host_advance_time(&test.host, 3000060);
        g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                                   DM_STM32H7_OTG_HFNUM), ==,
                         3);
        g_assert_cmpuint(test.channel_start_calls, ==, odd_frame[mode] ? 3 : 2);
        if (odd_frame[mode]) {
            g_assert_cmpint(test.request.pid, ==, first_pid);
            g_assert_cmpuint(dm_stm32h7_otg_host_read(
                                 &test.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                             64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
        }
    }
}

static void test_periodic_nak_halts_channel(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA |
                      (3u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) | 64;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar, 100);
    g_assert_cmpuint(test.channel_start_calls, ==, 1);

    dm_stm32h7_otg_host_complete_channel(
        &test.host, 0, DM_STM32H7_OTG_HOST_CHANNEL_NAK, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCCHAR(0)) &
                     DM_STM32H7_OTG_HCCHAR_CHENA, ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_NAK |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
    g_assert_false(dm_stm32h7_otg_host_service_channel(&test.host, 0, 200));
}

static void test_high_speed_sof_interval(void)
{
    TestOtgHost test;

    test_init(&test);
    dm_stm32h7_otg_host_set_port_connected(&test.host, true,
                                            DM_STM32H7_OTG_PORT_HIGH_SPEED);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 50);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 60);
    dm_stm32h7_otg_host_advance_time(&test.host, 125059);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFNUM), ==, 0);
    dm_stm32h7_otg_host_advance_time(&test.host, 125060);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFNUM), ==, 1);
}

static void test_port_power_stops_sof_until_reset_release(void)
{
    TestOtgHost test;

    test_init(&test);
    test_enable_port(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0, 0, 70);
    dm_stm32h7_otg_host_advance_time(&test.host, 2000000);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFNUM), ==, 0);

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 2000010);
    dm_stm32h7_otg_host_advance_time(&test.host, 3000010);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFNUM), ==, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 3000020);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 3000030);
    dm_stm32h7_otg_host_advance_time(&test.host, 4000030);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&test.host,
                                               DM_STM32H7_OTG_HFNUM), ==, 1);
}

static void test_hcfifo_word_order_and_reset(void)
{
    TestOtgHost test;
    uint8_t output[3] = { 0 };

    test_init(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCFIFO(0),
                               0x00434241, 0);
    g_assert_true(dm_stm32h7_otg_host_read_out_fifo(&test.host, 0, output,
                                                    sizeof(output)));
    g_assert_cmpmem(output, sizeof(output), "ABC", sizeof(output));
    dm_stm32h7_otg_host_write_in_fifo(&test.host, 0,
                                       (const uint8_t *)"WXYZ", 4);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&test.host,
                                              DM_STM32H7_OTG_HCFIFO(0)), ==,
                    0x5a595857);
    dm_stm32h7_otg_host_write_in_fifo(&test.host, 0,
                                       (const uint8_t *)"Q", 1);
    dm_stm32h7_otg_host_reset(&test.host);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&test.host,
                                              DM_STM32H7_OTG_HCFIFO(0)), ==, 0);
}

static void test_channel_disable_halts_and_port_gates_issue(void)
{
    TestOtgHost test;
    uint32_t hctsiz = 64 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;

    test_init(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCTSIZ(1),
                               hctsiz, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(1),
                               hcchar, 100);
    g_assert_cmpuint(test.channel_start_calls, ==, 0);
    test_enable_port(&test);
    g_assert_true(dm_stm32h7_otg_host_service_channel(&test.host, 1, 200));
    g_assert_cmpuint(test.channel_start_calls, ==, 1);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCCHAR(1),
                               DM_STM32H7_OTG_HCCHAR_CHDIS, 300);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCCHAR(1)) &
                     DM_STM32H7_OTG_HCCHAR_CHENA, ==, 0);
    g_assert_true(dm_stm32h7_otg_host_read(
                         &test.host, DM_STM32H7_OTG_HCINT(1)) &
                  DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_connect_change_irq_and_w1c(void)
{
    TestOtgHost test;
    uint32_t hprt;

    test_init(&test);
    dm_stm32h7_otg_host_set_port_connected(&test.host, true,
                                            DM_STM32H7_OTG_PORT_FULL_SPEED);
    hprt = dm_stm32h7_otg_host_read(&test.host, DM_STM32H7_OTG_HPRT0);
    g_assert_true(hprt & DM_STM32H7_OTG_HPRT0_CONNSTS);
    g_assert_true(hprt & DM_STM32H7_OTG_HPRT0_CONNDET);
    g_assert_cmpuint((hprt & DM_STM32H7_OTG_HPRT0_SPD_MASK) >>
                     DM_STM32H7_OTG_HPRT0_SPD_SHIFT, ==,
                     DM_STM32H7_OTG_PORT_FULL_SPEED);
    g_assert_false(test.irq_level);

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GINTMSK,
                               DM_STM32H7_OTG_GINTSTS_PRTINT, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_GINT, 0);
    g_assert_true(test.irq_level);
    g_assert_true(dm_stm32h7_otg_host_read(&test.host,
                                            DM_STM32H7_OTG_GINTSTS) &
                  DM_STM32H7_OTG_GINTSTS_PRTINT);

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_CONNDET, 0);
    g_assert_false(test.irq_level);
    g_assert_false(dm_stm32h7_otg_host_read(&test.host,
                                             DM_STM32H7_OTG_GINTSTS) &
                   DM_STM32H7_OTG_GINTSTS_PRTINT);
}

static void test_reset_deassert_notifies_attached_port(void)
{
    TestOtgHost test;
    uint32_t hprt;

    test_init(&test);
    dm_stm32h7_otg_host_set_port_connected(&test.host, true,
                                            DM_STM32H7_OTG_PORT_HIGH_SPEED);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 100);
    g_assert_cmpuint(test.reset_calls, ==, 0);
    g_assert_true(dm_stm32h7_otg_host_read(&test.host,
                                            DM_STM32H7_OTG_HPRT0) &
                  DM_STM32H7_OTG_HPRT0_RST);

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 123456789);
    hprt = dm_stm32h7_otg_host_read(&test.host, DM_STM32H7_OTG_HPRT0);
    g_assert_cmpuint(test.reset_calls, ==, 1);
    g_assert_cmpuint(test.reset_timestamp_ns, ==, 123456789);
    g_assert_false(hprt & DM_STM32H7_OTG_HPRT0_RST);
    g_assert_true(hprt & DM_STM32H7_OTG_HPRT0_ENA);
    g_assert_true(hprt & DM_STM32H7_OTG_HPRT0_ENACHG);
}

static void test_reset_without_attachment_is_not_delivered(void)
{
    TestOtgHost test;

    test_init(&test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 10);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 20);
    g_assert_cmpuint(test.reset_calls, ==, 0);
    g_assert_false(dm_stm32h7_otg_host_read(&test.host,
                                             DM_STM32H7_OTG_HPRT0) &
                   DM_STM32H7_OTG_HPRT0_ENA);
}

typedef struct TestOtgDwc2Boundary {
    DmStm32H7OtgHost host;
    DmUsbControlDevice control;
    DmUsbDwc2Device device;
    unsigned reset_calls;
    uint64_t reset_timestamp_ns;
} TestOtgDwc2Boundary;

static void test_dwc2_bus_reset(void *opaque, uint64_t timestamp_ns)
{
    TestOtgDwc2Boundary *test = opaque;

    ++test->reset_calls;
    test->reset_timestamp_ns = timestamp_ns;
    dm_usb_dwc2_bus_reset(&test->device, timestamp_ns);
}

static void test_host_port_reset_reaches_dwc2_bus_reset(void)
{
    TestOtgDwc2Boundary test = { 0 };
    uint32_t endpoint_control = DM_USB_DWC2_DXEPCTL_USBAEP |
                                DM_USB_DWC2_DXEPCTL_EPENA |
                                DM_USB_DWC2_DXEPCTL_STALL;

    dm_usb_control_init(&test.control, NULL, NULL, 64);
    dm_usb_dwc2_init(&test.device, &test.control, NULL, NULL, 64);
    dm_stm32h7_otg_host_init(&test.host, test_dwc2_bus_reset, &test,
                              NULL, NULL);
    test.control.address = 19;
    test.control.configuration = 1;
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_DCFG,
                      19u << DM_USB_DWC2_DCFG_DAD_SHIFT, 4);
    dm_usb_dwc2_write(&test.device, DM_USB_DWC2_DIEPCTL0 +
                      DM_USB_DWC2_EP_STRIDE, endpoint_control, 4);

    dm_stm32h7_otg_host_set_port_connected(&test.host, true,
                                            DM_STM32H7_OTG_PORT_FULL_SPEED);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 5);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 7654321);

    g_assert_cmpuint(test.reset_calls, ==, 1);
    g_assert_cmpuint(test.reset_timestamp_ns, ==, 7654321);
    g_assert_cmpuint(dm_usb_control_address(&test.control), ==, 0);
    g_assert_cmpuint(dm_usb_control_configuration(&test.control), ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_DCFG, 4) &
                     DM_USB_DWC2_DCFG_DAD_MASK, ==, 0);
    g_assert_cmpuint(dm_usb_dwc2_read(&test.device, DM_USB_DWC2_DIEPCTL0 +
                                      DM_USB_DWC2_EP_STRIDE, 4) &
                     (DM_USB_DWC2_DXEPCTL_EPENA | DM_USB_DWC2_DXEPCTL_STALL),
                     ==, 0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-stm32h7-otg-host/reset-defaults", test_reset_defaults);
    g_test_add_func("/dm-stm32h7-otg-host/connect-change-irq-w1c",
                    test_connect_change_irq_and_w1c);
    g_test_add_func("/dm-stm32h7-otg-host/channel-issue-complete-irq",
                    test_channel_issue_complete_and_irq);
    g_test_add_func("/dm-stm32h7-otg-host/deferred-multi-channel-irq-masks",
                    test_deferred_multi_channel_irq_masks);
    g_test_add_func("/dm-stm32h7-otg-host/channel-nak-serviceable",
                    test_channel_nak_is_serviceable);
    g_test_add_func("/dm-stm32h7-otg-host/channel-multi-packet-service",
                    test_channel_multi_packet_requires_service);
    g_test_add_func("/dm-stm32h7-otg-host/channel-multi-packet-short-stop",
                    test_channel_multi_packet_short_stops);
    g_test_add_func("/dm-stm32h7-otg-host/channel-zero-length-completion",
                    test_channel_zero_length_completion_preserves_packet_count);
    g_test_add_func("/dm-stm32h7-otg-host/channel-short-completion-packet-count",
                    test_channel_short_completion_consumes_one_packet);
    g_test_add_func("/dm-stm32h7-otg-host/disconnect-cancels-completion",
                    test_disconnect_cancels_pending_completion);
    g_test_add_func("/dm-stm32h7-otg-host/port-power-off-cancels-completion",
                    test_port_power_off_cancels_pending_completion);
    g_test_add_func("/dm-stm32h7-otg-host/hcdma-progress-controller-reset",
                    test_hcdma_progress_and_controller_reset);
    g_test_add_func("/dm-stm32h7-otg-host/sof-frame-channel-schedule",
                    test_sof_frame_and_channel_schedule);
    g_test_add_func("/dm-stm32h7-otg-host/periodic-sof-frame-parity",
                    test_periodic_sof_frame_parity);
    g_test_add_func("/dm-stm32h7-otg-host/periodic-nak-halts-channel",
                    test_periodic_nak_halts_channel);
    g_test_add_func("/dm-stm32h7-otg-host/high-speed-sof-interval",
                    test_high_speed_sof_interval);
    g_test_add_func("/dm-stm32h7-otg-host/port-power-stops-sof",
                    test_port_power_stops_sof_until_reset_release);
    g_test_add_func("/dm-stm32h7-otg-host/hcfifo-word-order-reset",
                    test_hcfifo_word_order_and_reset);
    g_test_add_func("/dm-stm32h7-otg-host/channel-disable-port-gate",
                    test_channel_disable_halts_and_port_gates_issue);
    g_test_add_func("/dm-stm32h7-otg-host/reset-deassert-notifies",
                    test_reset_deassert_notifies_attached_port);
    g_test_add_func("/dm-stm32h7-otg-host/reset-without-attachment",
                    test_reset_without_attachment_is_not_delivered);
    g_test_add_func("/dm-stm32h7-otg-host/dwc2-bus-reset-boundary",
                    test_host_port_reset_reaches_dwc2_bus_reset);
    return g_test_run();
}
