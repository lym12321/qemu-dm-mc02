/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"
#include "qapi/qmp/qdict.h"

#define DTCM_BASE          0x20000000ull
#define GPIOA_BASE         0x58020000ull
#define GPIOB_BASE         0x58020400ull
#define GPIOC_BASE         0x58020800ull
#define GPIO_MODER         0x00
#define GPIO_IDR           0x10
#define GPIO_ODR           0x14
#define TIM2_BASE          0x40000000ull
#define TIM2_CR1           (TIM2_BASE + 0x00)
#define SPI2_BASE          0x40003800ull
#define SPI2_CR1           (SPI2_BASE + 0x00)
#define CORDIC_BASE        0x58004400ull
#define CORDIC_CSR         (CORDIC_BASE + 0x00)
#define CORDIC_WDATA       (CORDIC_BASE + 0x04)
#define CORDIC_RDATA       (CORDIC_BASE + 0x08)
#define USB_GRXFSIZ        0x40040024ull
#define FDCAN_MSG_RAM      0x4000ac55ull
#define DTCM_MAGIC         0xa55a5aa5u
#define GPIO_POWER_OUTPUTS ((1u << (13 * 2)) | (1u << (14 * 2)) | \
                           (1u << (15 * 2)))
#define GPIO_POWER_LATCHES ((1u << 13) | (1u << 14) | (1u << 15))
#define GPIO_BMI_CS_INACTIVE ((1u << 0) | (1u << 3))
#define EXTI_FTSR1         0x58000004ull
#define EXTI_PR1           0x58000088ull
#define AXI_WAVEFORM       0x24000100ull
#define DMA2_S6CR          0x400204a0ull
#define DMA2_S6NDTR        0x400204a4ull
#define DMA2_S6PAR         0x400204a8ull
#define DMA2_S6M0AR        0x400204acull

static void set_bool(QTestState *qts, const char *property, bool value)
{
    qtest_qom_set_bool(qts, "/machine", property, value);
}

static void set_string(QTestState *qts, const char *property,
                        const char *value)
{
    qtest_qmp_assert_success(qts,
        "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': %s, 'value': %s } }", property, value);
}

static void test_warm_reset_reprojects_board_state(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t rx_fifo_reset = qtest_readl(qts, USB_GRXFSIZ);

    qtest_writel(qts, DTCM_BASE, DTCM_MAGIC);
    qtest_writel(qts, TIM2_CR1, 1);
    qtest_writel(qts, SPI2_CR1, 1);
    qtest_writel(qts, CORDIC_CSR, 1u << 19); /* Paired result pending. */
    qtest_writel(qts, CORDIC_WDATA, 0x20000000);
    qtest_writel(qts, USB_GRXFSIZ, 77);
    qtest_writeb(qts, FDCAN_MSG_RAM, 0xa5);
    set_bool(qts, "user-key", true);
    set_bool(qts, "electrical-power", true);
    qtest_writel(qts, GPIOC_BASE + GPIO_MODER, GPIO_POWER_OUTPUTS);
    qtest_writel(qts, GPIOC_BASE + GPIO_ODR, GPIO_POWER_LATCHES);
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* Warm reset keeps ordinary SoC RAM but resets peripheral state. */
    g_assert_cmphex(qtest_readl(qts, DTCM_BASE), ==, DTCM_MAGIC);
    g_assert_cmphex(qtest_readl(qts, TIM2_CR1), ==, 0);
    g_assert_cmphex(qtest_readl(qts, SPI2_CR1), ==, 0);
    g_assert_cmphex(qtest_readl(qts, CORDIC_CSR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, CORDIC_RDATA), ==, 0);
    g_assert_cmphex(qtest_readl(qts, USB_GRXFSIZ), ==, rx_fifo_reset);
    g_assert_cmphex(qtest_readb(qts, FDCAN_MSG_RAM), ==, 0);

    /* Board CS deassertion and external key input are re-projected after
     * generic GPIO/EXTI reset. */
    g_assert_cmphex(qtest_readl(qts, GPIOC_BASE + GPIO_ODR), ==,
                    GPIO_BMI_CS_INACTIVE);
    g_assert_cmphex(qtest_readl(qts, GPIOA_BASE + GPIO_IDR) & (1u << 15), ==,
                    0);
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "user-key"));

    /* The electrical policy remains enabled, but reset GPIO latches remove
     * switched 5 V and the downstream CAN/RS485 consumers are powered off. */
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "out1-enabled"));
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "out2-enabled"));
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "5v-switch-enabled"));
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "mcu-power-good"));
    qtest_quit(qts);
}

static void test_cold_reset_clears_volatile_memory(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    qtest_writel(qts, DTCM_BASE, DTCM_MAGIC);
    set_bool(qts, "cold-reset", true);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, DTCM_BASE), ==, 0);
    qtest_quit(qts);
}

static QDict *board_state(QTestState *qts)
{
    QDict *response = qtest_qmp(qts,
        "{ 'execute': 'qom-get', 'arguments': { 'path': '/machine', "
        "'property': 'board-state' } }");
    QDict *state = qdict_get_qdict(response, "return");

    g_assert_nonnull(state);
    qobject_ref(state);
    qobject_unref(response);
    return state;
}

static void assert_gpio_bank(QDict *state, const char *port, uint32_t moder,
                             uint16_t sampled, uint16_t odr,
                             uint16_t output_mask)
{
    QDict *bank = qdict_get_qdict(qdict_get_qdict(state, "gpio"), port);

    g_assert_nonnull(bank);
    g_assert_cmpuint(qdict_size(bank), ==, 4);
    g_assert_cmphex(qdict_get_int(bank, "moder"), ==, moder);
    g_assert_cmphex(qdict_get_int(bank, "sampled"), ==, sampled);
    g_assert_cmphex(qdict_get_int(bank, "odr"), ==, odr);
    g_assert_cmphex(qdict_get_int(bank, "output_mask"), ==, output_mask);
}

static void assert_led(QDict *state, const char *pin, uint32_t rgb,
                        uint8_t brightness, const char *source)
{
    QDict *led = qdict_get_qdict(state, "led");

    g_assert_nonnull(led);
    g_assert_cmpstr(qdict_get_str(led, "pin"), ==, pin);
    g_assert_cmphex(qdict_get_int(led, "rgb"), ==, rgb);
    g_assert_cmpuint(qdict_get_int(led, "brightness"), ==, brightness);
    g_assert_cmpstr(qdict_get_str(led, "source"), ==, source);
}

static void assert_key(QDict *state, const char *pin, bool pressed, int level)
{
    QDict *key = qdict_get_qdict(state, "key");

    g_assert_nonnull(key);
    g_assert_cmpstr(qdict_get_str(key, "pin"), ==, pin);
    g_assert_cmpint(qdict_get_bool(key, "pressed"), ==, pressed);
    g_assert_cmpint(qdict_get_int(key, "level"), ==, level);
}

static void test_board_state_gpio_key_and_time(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    QDict *state;
    QDict *again;
    QDict *response;
    int64_t initial_time;
    int64_t now;

    /* RM0468 Rev 3 GPIO MODER/IDR: GPIO output mode is 01.  AF and analog
     * below retain the existing model's explicit digital input approximation.
     * Independent literals: B1/B4 output, B2 AF, B3 analog. */
    qtest_writel(qts, GPIOB_BASE + GPIO_MODER, 0x1e4);
    qtest_writel(qts, GPIOB_BASE + GPIO_ODR, 2);
    set_string(qts, "gpio-input", "B0=1");
    set_string(qts, "gpio-input", "B2=1");
    set_string(qts, "gpio-input", "B3=1");
    set_string(qts, "gpio-input", "B4=1");
    qtest_writel(qts, GPIOA_BASE + GPIO_MODER, 0x4000); /* A7 output. */
    qtest_writel(qts, GPIOA_BASE + GPIO_ODR, 0x80);
    set_string(qts, "vin-mv", "25000");

    state = board_state(qts);
    g_assert_cmpuint(qdict_get_int(state, "schema"), ==, 1);
    g_assert_cmpstr(qdict_get_str(state, "board"), ==, "DM-MC02");
    g_assert_cmpuint(qdict_get_int(state, "vin_mv"), ==, 25000);
    g_assert_cmpuint(qdict_size(qdict_get_qdict(state, "gpio")), ==, 8);
    assert_gpio_bank(state, "B", 0x1e4, 0x0f, 2, 0x12);
    assert_gpio_bank(state, "A", 0x4000, 0x8080, 0x80, 0x80);
    assert_gpio_bank(state, "H", 0, 0, 0, 0);
    assert_led(state, "A7", 0xffffff, 255, "gpio-latch");
    assert_key(state, "A15", false, 1);
    initial_time = qdict_get_int(state, "timestamp_ns");
    g_assert_cmphex(qtest_readl(qts, GPIOB_BASE + GPIO_IDR), ==, 0x0f);
    g_assert_cmphex(qtest_readl(qts, GPIOB_BASE + GPIO_MODER), ==, 0x1e4);
    g_assert_cmphex(qtest_readl(qts, GPIOB_BASE + GPIO_ODR), ==, 2);
    qobject_unref(state);

    /* Board input must reach the real IDR/EXTI consumer.  RM0468 and H723
     * CMSIS place EXTI PR1 at 0x88 and specify write-one-to-clear; observing
     * the board must not acknowledge this pending falling edge. */
    qtest_writel(qts, EXTI_FTSR1, 0x8000);
    set_bool(qts, "user-key", true);
    g_assert_cmphex(qtest_readl(qts, GPIOA_BASE + GPIO_IDR), ==, 0x80);
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, 0x8000);
    state = board_state(qts);
    assert_key(state, "A15", true, 0);
    assert_gpio_bank(state, "A", 0x4000, 0x80, 0x80, 0x80);
    again = board_state(qts);
    g_assert_true(qobject_is_equal(QOBJECT(state), QOBJECT(again)));
    qobject_unref(again);
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, 0x8000);

    response = qtest_qmp(qts,
        "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'board-state', 'value': {} } }");
    g_assert_nonnull(qdict_get_qdict(response, "error"));
    g_assert_nonnull(strstr(qdict_get_str(qdict_get_qdict(response, "error"),
                                        "desc"), "not writable"));
    qobject_unref(response);
    again = board_state(qts);
    g_assert_true(qobject_is_equal(QOBJECT(state), QOBJECT(again)));
    qobject_unref(again);
    qobject_unref(state);

    now = qtest_clock_step(qts, 1234567);
    state = board_state(qts);
    g_assert_cmpint(qdict_get_int(state, "timestamp_ns"), ==, now);
    g_assert_cmpint(now, ==, initial_time + 1234567);
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, 0x8000);
    qobject_unref(state);

    set_string(qts, "gpio-input", "A15=1");
    state = board_state(qts);
    assert_key(state, "A15", false, 1);
    g_assert_cmphex(qtest_readl(qts, GPIOA_BASE + GPIO_IDR), ==, 0x8080);
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, 0x8000);
    qobject_unref(state);
    qtest_quit(qts);
}

static void test_board_state_profile_aliases(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,board-profile=STM32H723-EVAL");
    QDict *state;

    qtest_writel(qts, GPIOA_BASE + GPIO_MODER, 1); /* EVAL LED is A0. */
    qtest_writel(qts, GPIOA_BASE + GPIO_ODR, 1);
    state = board_state(qts);
    g_assert_cmpstr(qdict_get_str(state, "board"), ==, "STM32H723-EVAL");
    assert_gpio_bank(state, "A", 1, 0x2001, 1, 1);
    assert_led(state, "A0", 0xffffff, 255, "gpio-latch");
    assert_key(state, "A13", false, 1);
    qobject_unref(state);

    qtest_writel(qts, EXTI_FTSR1, 0x2000);
    set_string(qts, "gpio-input", "A13=0");
    state = board_state(qts);
    assert_key(state, "A13", true, 0);
    assert_gpio_bank(state, "A", 1, 1, 1, 1);
    g_assert_cmphex(qtest_readl(qts, GPIOA_BASE + GPIO_IDR), ==, 1);
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, 0x2000);
    qobject_unref(state);
    qtest_quit(qts);
}

static void test_board_state_ws2812_observation(void)
{
    /* Literal GRB, MSB-first halfword waveforms from the existing board
     * fixture's 168/84 duty values.  These expectations do not invoke the
     * production encoder/decoder.  AXI SRAM is DMA-visible in RM0468 §2.1.6. */
    static const uint16_t red[] = {
         84,  84,  84,  84,  84,  84,  84,  84,
        168, 168, 168, 168, 168, 168, 168, 168,
         84,  84,  84,  84,  84,  84,  84,  84,
    };
    static const uint16_t green[] = {
        168, 168, 168, 168, 168, 168, 168, 168,
         84,  84,  84,  84,  84,  84,  84,  84,
         84,  84,  84,  84,  84,  84,  84,  84,
    };
    QTestState *qts = qtest_init("-machine dm-mc02");
    QDict *state;
    uint8_t before[sizeof(red)];
    uint8_t after[sizeof(red)];
    int64_t time;

    for (unsigned i = 0; i < ARRAY_SIZE(red); ++i) {
        qtest_writew(qts, AXI_WAVEFORM + 2 * i, red[i]);
    }
    qtest_writel(qts, DMA2_S6PAR, 0x40010400);
    qtest_writel(qts, DMA2_S6M0AR, AXI_WAVEFORM);
    qtest_writel(qts, DMA2_S6NDTR, 24);
    /* EN, MINC, 16-bit peripheral/memory widths; no DMA request is fired. */
    qtest_writel(qts, DMA2_S6CR, 0x2c41);
    qtest_memread(qts, AXI_WAVEFORM, before, sizeof(before));
    state = board_state(qts);
    assert_led(state, "A7", 0xff0000, 255, "ws2812-dma");
    time = qdict_get_int(state, "timestamp_ns");
    qobject_unref(state);
    qtest_memread(qts, AXI_WAVEFORM, after, sizeof(after));
    g_assert_cmpmem(before, sizeof(before), after, sizeof(after));
    g_assert_cmphex(qtest_readl(qts, DMA2_S6CR), ==, 0x2c41);
    g_assert_cmphex(qtest_readl(qts, DMA2_S6NDTR), ==, 24);
    g_assert_cmphex(qtest_readl(qts, DMA2_S6PAR), ==, 0x40010400);
    g_assert_cmphex(qtest_readl(qts, DMA2_S6M0AR), ==, AXI_WAVEFORM);

    /* A same-time RAM update must be observed without a cache refresh,
     * register rewrite or DMA beat.  The getter remains an observer. */
    for (unsigned i = 0; i < ARRAY_SIZE(green); ++i) {
        qtest_writew(qts, AXI_WAVEFORM + 2 * i, green[i]);
    }
    qtest_memread(qts, AXI_WAVEFORM, before, sizeof(before));
    state = board_state(qts);
    assert_led(state, "A7", 0x00ff00, 255, "ws2812-dma");
    g_assert_cmpint(qdict_get_int(state, "timestamp_ns"), ==, time);
    qobject_unref(state);
    qtest_memread(qts, AXI_WAVEFORM, after, sizeof(after));
    g_assert_cmpmem(before, sizeof(before), after, sizeof(after));
    g_assert_cmphex(qtest_readl(qts, DMA2_S6NDTR), ==, 24);
    g_assert_cmphex(qtest_readl(qts, DMA2_S6CR), ==, 0x2c41);
    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/board-reset/warm-state",
                    test_warm_reset_reprojects_board_state);
    g_test_add_func("/dm-mc02/board-reset/cold-volatile-memory",
                    test_cold_reset_clears_volatile_memory);
    g_test_add_func("/dm-mc02/board-state/gpio-key-time",
                    test_board_state_gpio_key_and_time);
    g_test_add_func("/dm-mc02/board-state/profile-aliases",
                    test_board_state_profile_aliases);
    g_test_add_func("/dm-mc02/board-state/ws2812-observation",
                    test_board_state_ws2812_observation);
    return g_test_run();
}
