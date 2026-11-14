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
#define NVIC_IPR          0xe000e400ull
#define NVIC_SHPR3        0xe000ed20ull

static void check_priority_width(const char *args, uint32_t ipr,
                                  uint32_t shpr, uint8_t mask)
{
    QTestState *qts = qtest_init(args);

    /* Native byte/word paths and both priority banks share implemented bits. */
    qtest_writel(qts, NVIC_IPR, 0x12345678);
    g_assert_cmphex(qtest_readl(qts, NVIC_IPR), ==, ipr);
    qtest_writeb(qts, NVIC_IPR, 0xff);
    g_assert_cmphex(qtest_readb(qts, NVIC_IPR), ==, mask);
    qtest_writel(qts, NVIC_SHPR3, 0x9a730000);
    g_assert_cmphex(qtest_readl(qts, NVIC_SHPR3), ==, shpr);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, NVIC_IPR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, NVIC_SHPR3), ==, 0);
    qtest_writeb(qts, NVIC_IPR, 0xff);
    g_assert_cmphex(qtest_readb(qts, NVIC_IPR), ==, mask);
    qtest_quit(qts);
}

static void test_native_nvic_configured_width(void)
{
    QTestState *qts;

    check_priority_width("-machine mps2-an500 -global armv7m.num-prio-bits=4",
                         0x10305070, 0x90700000, 0xf0);
    qts = qtest_init("-machine mps2-an500 -global armv7m.num-prio-bits=4");
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine/armv7m', "
        "'property': 'num-prio-bits', 'value': 8 } }"));
    qtest_writeb(qts, NVIC_IPR, 0xff);
    g_assert_cmphex(qtest_readb(qts, NVIC_IPR), ==, 0xf0);
    qtest_quit(qts);
}

static void test_native_nvic_default_width(void)
{
    check_priority_width("-machine mps2-an500", 0x12345678, 0x9a730000, 0xff);
}

static void test_native_nvic_v6_width(void)
{
    check_priority_width("-machine microbit", 0x00004040, 0x80400000, 0xc0);
}

static void test_h723_nvic_width(void)
{
    /* ST CMSIS stm32h723xx.h __NVIC_PRIO_BITS=4, independent of firmware. */
    check_priority_width("-machine dm-mc02", 0x10305070, 0x90700000, 0xf0);
}

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
    assert_led(state, "A7", 0, 0, "ws2812-din");
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
    assert_led(state, "A0", 0, 0, "ws2812-din");
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
    QTestState *qts = qtest_init("-machine dm-mc02");
    QDict *state, *again;

    /* Buffer edits and static GPIO levels are not WS2812 frames. */
    qtest_writel(qts, DMA2_S6M0AR, AXI_WAVEFORM);
    qtest_writel(qts, DMA2_S6NDTR, 24);
    qtest_writel(qts, DMA2_S6CR, 0x2c41);
    qtest_writel(qts, GPIOA_BASE + GPIO_MODER, 1u << 14);
    qtest_writel(qts, GPIOA_BASE + GPIO_ODR, 0x80);
    qtest_clock_step(qts, 100000);
    state = board_state(qts);
    assert_led(state, "A7", 0, 0, "ws2812-din");
    qobject_unref(state);
    qtest_writel(qts, GPIOA_BASE + GPIO_ODR, 0);
    qtest_clock_step(qts, 50000);
    /* Independent Worldsemi timing literals, GRB 00 12 34 => RGB12 00 34. */
    for (int bit = 23; bit >= 0; --bit) {
        unsigned high_ns = (0x001234 & (1u << bit)) ? 800 : 400;
        qtest_writel(qts, GPIOA_BASE + 0x18, 0x80);
        qtest_clock_step(qts, high_ns);
        qtest_writel(qts, GPIOA_BASE + 0x18, 0x800000);
        qtest_clock_step(qts, 1250 - high_ns);
    }
    state = board_state(qts);
    assert_led(state, "A7", 0, 0, "ws2812-din");
    qobject_unref(state);
    /* Last zero fell 850ns before the loop ended. An earlier scheduled check
     * must rearm, not latch early or require another GPIO edge. */
    qtest_clock_step(qts, 49149);
    state = board_state(qts);
    assert_led(state, "A7", 0, 0, "ws2812-din");
    qobject_unref(state);
    qtest_clock_step(qts, 1);
    state = board_state(qts);
    assert_led(state, "A7", 0x120034, 0x34, "ws2812-din");
    g_assert_cmpuint(qdict_get_int(qdict_get_qdict(state, "led"), "frames"), ==, 1);
    g_assert_cmpuint(qdict_get_int(qdict_get_qdict(state, "led"), "latched_ns"),
                     ==, qdict_get_int(state, "timestamp_ns"));
    again = board_state(qts);
    g_assert_true(qobject_is_equal(QOBJECT(state), QOBJECT(again)));
    qobject_unref(again);
    qobject_unref(state);
    g_assert_cmphex(qtest_readl(qts, DMA2_S6NDTR), ==, 24);
    qtest_quit(qts);
}

static void test_board_state_ws2812_spi(void)
{
    const uint32_t spi = 0x40013000;
    const uint32_t grb = 0x12ab34;
    for (unsigned variant = 0; variant < 4; ++variant) {
        unsigned controller = variant / 2;
        unsigned symbol_bits = variant % 2 ? 10 : 5;
        unsigned zero_high = variant % 2 ? 3 : 2;
        unsigned one_high = variant % 2 ? 6 : 3;
        unsigned mbr = variant % 2 ? 2 : 3;
        unsigned byte_count = variant % 2 ? 110 : 55;
        unsigned stream = controller ? 5 : 3;
        uint32_t dma = 0x40020010 + controller * 0x400 + stream * 0x18;
        uint8_t bytes[110] = { 0 };
        QTestState *qts = qtest_init("-machine dm-mc02");
        QDict *state;

        /* Two independent encodings cross SPI byte boundaries:
         * 4MHz: 11000/11100; 8MHz: 1110000000/1111110000.
         * A second white pixel keeps DIN active beyond the first 50us check. */
        for (unsigned bit = 0; bit < 48; ++bit) {
            unsigned high = (bit >= 24 || (grb & (1u << (23 - bit)))) ?
                            one_high : zero_high;
            for (unsigned j = 0; j < high; ++j) {
                unsigned index = bit * symbol_bits + j;
                bytes[index / 8] |= 1u << (7 - index % 8);
            }
        }
        qtest_memwrite(qts, AXI_WAVEFORM, bytes, byte_count);
        qtest_writel(qts, GPIOA_BASE + 0xc, 2u << 14);
        qtest_writel(qts, GPIOA_BASE + 0x20, 5u << 28); /* SPI1 MOSI AF5 */
        qtest_writel(qts, GPIOA_BASE, 2u << 14);
        qtest_writel(qts, 0x580244f0, 1u << 12); /* SPI1EN */
        qtest_writel(qts, spi + 8, 7 | (mbr << 28) | (1u << 15));
        qtest_writel(qts, spi + 12, (1u << 22) | (1u << 17) | (1u << 26));
        qtest_writel(qts, spi + 4, byte_count);
        qtest_writel(qts, spi + 0x10, 8); /* EOTIE */
        qtest_writel(qts, 0xe000e104, 8); /* NVIC SPI1 IRQ35 */
        qtest_writel(qts, dma + 8, spi + 0x20);
        qtest_writel(qts, dma + 12, AXI_WAVEFORM);
        qtest_writel(qts, dma + 4, byte_count);
        qtest_writel(qts, 0x40020800 + (controller * 8 + stream) * 4, 38);
        qtest_writel(qts, dma, 1 | (1u << 6) | (1u << 10));
        qtest_writel(qts, spi, 1 | (1u << 9));
        /* Reset PLL1Q is unavailable. No synthetic clock or early EOT. */
        qtest_clock_step(qts, 60000);
        g_assert_cmpuint(qtest_readl(qts, dma + 4), ==, byte_count);
        g_assert_false(qtest_readl(qts, spi + 0x14) & 8);
        qtest_writel(qts, 0x58024450, 4u << 12); /* per_ck = HSI64 */
        qtest_clock_step(qts, 10000);
        qtest_writel(qts, GPIOA_BASE + 0x18, 1); /* Other pin, same AF route. */
        qtest_writel(qts, GPIOB_BASE + GPIO_ODR, 1); /* Unrelated bank. */
        qtest_clock_step(qts, 99499);
        state = board_state(qts);
        assert_led(state, "A7", 0, 0, "ws2812-din");
        qobject_unref(state);
        qtest_clock_step(qts, 1); /* Last fall at 119500ns + reset low 50000ns. */
        state = board_state(qts);
        assert_led(state, "A7", 0xab1234, 0xab, "ws2812-din");
        g_assert_cmpuint(qdict_get_int(qdict_get_qdict(state, "led"), "frames"), ==, 1);
        g_assert_cmpuint(qdict_get_int(qdict_get_qdict(state, "led"), "errors"), ==, 0);
        g_assert_cmpuint(qdict_get_int(qdict_get_qdict(state, "led"), "latched_ns"), ==, 169500);
        qobject_unref(state);
        qtest_clock_step(qts, 1000);
        g_assert_cmpuint(qtest_readl(qts, dma + 4), ==, 0);
        g_assert_true(qtest_readl(qts, spi + 0x14) & 8);
        g_assert_true(qtest_readl(qts, 0xe000e204) & 8);
        /* RCC reset clears the real peripheral and deasserts its IRQ. */
        qtest_writel(qts, 0x58024498, 1u << 12);
        g_assert_cmphex(qtest_readl(qts, spi), ==, 0);
        g_assert_cmphex(qtest_readl(qts, spi + 8), ==, 0x00070007);
        qtest_quit(qts);
    }
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/board-state/ws2812-spi", test_board_state_ws2812_spi);
    if (qtest_has_machine("mps2-an500")) {
        g_test_add_func("/armv7m/nvic/configured-priority-width",
                        test_native_nvic_configured_width);
        g_test_add_func("/armv7m/nvic/default-priority-width",
                        test_native_nvic_default_width);
    }
    if (qtest_has_machine("microbit")) {
        g_test_add_func("/armv7m/nvic/v6-priority-width", test_native_nvic_v6_width);
    }
    g_test_add_func("/dm-mc02/nvic/h723-priority-width", test_h723_nvic_width);
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
