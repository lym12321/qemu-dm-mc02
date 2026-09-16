#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_board.h"

#include <stdio.h>
#include <stdlib.h>

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "board profile smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static void set_gpio_output(DmMc02BoardGpioState *gpio, unsigned pin,
                            bool high)
{
    gpio->moder &= ~(UINT32_C(3) << (pin * 2));
    gpio->moder |= UINT32_C(1) << (pin * 2);
    if (high) {
        gpio->odr |= UINT32_C(1) << pin;
    } else {
        gpio->odr &= ~(UINT32_C(1) << pin);
    }
}

static void set_gpio_af(DmMc02BoardGpioState *gpio, unsigned pin,
                        unsigned af, bool high)
{
    unsigned shift = (pin & 7u) * 4u;
    uint32_t *afr = pin < 8 ? &gpio->afr0 : &gpio->afr1;

    gpio->moder &= ~(UINT32_C(3) << (pin * 2));
    gpio->moder |= UINT32_C(2) << (pin * 2);
    *afr &= ~(UINT32_C(0xf) << shift);
    *afr |= (uint32_t)af << shift;
    if (high) {
        gpio->odr |= UINT32_C(1) << pin;
    } else {
        gpio->odr &= ~(UINT32_C(1) << pin);
    }
}

static void test_profile(void)
{
    const DmMc02BoardProfile *board = dm_mc02_board_dm_mc02();
    const DmMc02BoardProfile *eval = dm_mc02_board_lookup("STM32H723-EVAL");
    Error *err = NULL;

    CHECK(board != NULL);
    CHECK(dm_mc02_board_validate(board, &err));
    CHECK(err == NULL);
    CHECK(dm_mc02_board_lookup(NULL) == board);
    CHECK(dm_mc02_board_lookup("DM-MC02") == board);
    CHECK(eval != NULL);
    CHECK(eval != board);
    CHECK(strcmp(board->soc->cpu_type,
                DM_MC02_ARMV7M_CORTEX_M7_CPU_TYPE) == 0);
    CHECK(strcmp(eval->soc->cpu_type,
                DM_MC02_ARMV7M_CORTEX_M7_CPU_TYPE) == 0);
    CHECK(dm_mc02_board_validate(eval, &err));
    CHECK(err == NULL);
    CHECK(eval->soc == board->soc);
    CHECK(eval->uart_count == 2);
    CHECK(eval->fdcan_count == 1);
    CHECK(board->flash_profile_count == 1);
    CHECK(strcmp(board->flash_profiles[0].name, "W25Q64JV") == 0);
    CHECK(board->flash_profiles[0].ospi_index == 2);
    CHECK(board->flash_profiles[0].storage_size == 8 * 1024 * 1024);
    CHECK(board->flash_profiles[0].memory_mapped);
    CHECK(eval->flash_profile_count == 0);
}

static void test_gpio_snapshot(void)
{
    const DmMc02BoardProfile *board = dm_mc02_board_dm_mc02();
    DmMc02BoardGpioState gpio[DM_MC02_BOARD_GPIO_BANK_COUNT] = { 0 };
    DmMc02BoardSignals signals;

    /* LED A7, buzzer B15, BMI088 CS C0/C3, and both RS485 DE pins. */
    set_gpio_output(&gpio[0], 7, true);
    set_gpio_output(&gpio[1], 15, true);
    set_gpio_output(&gpio[2], 0, true);
    set_gpio_output(&gpio[2], 3, false);
    set_gpio_output(&gpio[3], 4, true);
    set_gpio_af(&gpio[1], 14, 7, true);

    gpio[2].odr |= (UINT32_C(1) << 13) | (UINT32_C(1) << 15);
    CHECK(dm_mc02_board_decode_gpio_outputs(board, gpio, ARRAY_SIZE(gpio),
                                             0, &signals));
    CHECK(signals.power_gpio_odr == gpio[2].odr);
    CHECK(signals.led_high);
    CHECK(signals.led_dirty);
    CHECK(signals.buzzer_gpio_output);
    CHECK(signals.buzzer_gpio_high);
    CHECK(!signals.buzzer_af_active);
    CHECK(signals.rs485[1].mode == DM_MC02_RS485_DE_MANUAL_GPIO);
    CHECK(signals.rs485[1].level);
    CHECK(signals.rs485[2].mode == DM_MC02_RS485_DE_AUTO_USART);
    CHECK(signals.rs485[2].level);

    /* Timer AF takes precedence over the buzzer's GPIO-level interpretation. */
    set_gpio_af(&gpio[1], 15, 2, false);
    set_gpio_output(&gpio[3], 4, false);
    gpio[1].odr &= ~(UINT32_C(1) << 14);
    CHECK(dm_mc02_board_decode_gpio_outputs(board, gpio, ARRAY_SIZE(gpio),
                                             2, &signals));
    CHECK(signals.led_high);
    CHECK(!signals.led_dirty);
    CHECK(!signals.buzzer_gpio_output);
    CHECK(!signals.buzzer_gpio_high);
    CHECK(signals.buzzer_af_active);
    CHECK(signals.rs485[1].mode == DM_MC02_RS485_DE_MANUAL_GPIO);
    CHECK(!signals.rs485[1].level);
    CHECK(signals.rs485[2].mode == DM_MC02_RS485_DE_AUTO_USART);
    CHECK(!signals.rs485[2].level);
}

static void test_spi_cs_routes(void)
{
    const DmMc02BoardProfile *board = dm_mc02_board_dm_mc02();
    const DmMc02BoardProfile *eval = dm_mc02_board_lookup("STM32H723-EVAL");
    DmMc02BoardProfile active_high = *board;
    DmMc02BoardSpiCsRoute active_high_routes[2];
    DmMc02BoardGpioState gpio[DM_MC02_BOARD_GPIO_BANK_COUNT] = { 0 };
    uint32_t selected_mask;

    memcpy(active_high_routes, board->spi_cs_routes,
           sizeof(active_high_routes));
    active_high_routes[0].active_low = false;
    active_high.spi_cs_routes = active_high_routes;
    active_high.spi_cs_route_count = ARRAY_SIZE(active_high_routes);

    CHECK(dm_mc02_board_decode_spi_selected_mask(
        board, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 0);

    set_gpio_output(&gpio[2], 0, true);
    set_gpio_output(&gpio[2], 3, true);
    CHECK(dm_mc02_board_decode_spi_selected_mask(
        board, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 0);

    set_gpio_output(&gpio[2], 0, false);
    CHECK(dm_mc02_board_decode_spi_selected_mask(
        board, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 1u);

    set_gpio_output(&gpio[2], 3, false);
    CHECK(dm_mc02_board_decode_spi_selected_mask(
        board, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 3u);

    gpio[2].moder &= ~(UINT32_C(3) << (3 * 2));
    CHECK(dm_mc02_board_decode_spi_selected_mask(
        board, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 1u);

    CHECK(!dm_mc02_board_decode_spi_selected_mask(
        board, gpio, ARRAY_SIZE(gpio), 2, 1, &selected_mask));
    CHECK(dm_mc02_board_decode_spi_selected_mask(
        eval, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 0);

    memset(gpio, 0, sizeof(gpio));
    set_gpio_output(&gpio[2], 0, false);
    CHECK(dm_mc02_board_decode_spi_selected_mask(
        &active_high, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 0);
    set_gpio_output(&gpio[2], 0, true);
    CHECK(dm_mc02_board_decode_spi_selected_mask(
        &active_high, gpio, ARRAY_SIZE(gpio), 2, 4, &selected_mask));
    CHECK(selected_mask == 1u);
}

static void test_rejected_inputs(void)
{
    const DmMc02BoardProfile *board = dm_mc02_board_dm_mc02();
    DmMc02BoardProfile invalid = *board;
    DmMc02BoardSpiCsRoute routes[2];
    DmMc02BoardGpioState gpio[DM_MC02_BOARD_GPIO_BANK_COUNT] = { 0 };
    DmMc02BoardSignals signals;
    Error *err = NULL;

    invalid.devices.led.bank = DM_MC02_BOARD_PIN_NONE;
    CHECK(!dm_mc02_board_validate(&invalid, &err));
    CHECK(err != NULL);
    error_free(err);

    memcpy(routes, board->spi_cs_routes, sizeof(routes));
    routes[1].target_index = routes[0].target_index;
    invalid = *board;
    invalid.spi_cs_routes = routes;
    invalid.spi_cs_route_count = ARRAY_SIZE(routes);
    err = NULL;
    CHECK(!dm_mc02_board_validate(&invalid, &err));
    CHECK(err != NULL);
    error_free(err);

    CHECK(!dm_mc02_board_decode_gpio_outputs(
        board, gpio, board->soc->gpio_bank_count - 1, 0, &signals));

    {
        DmMc02BoardFlashProfile flashes[2];

        memcpy(flashes, board->flash_profiles, sizeof(flashes[0]));
        flashes[1] = flashes[0];
        invalid = *board;
        invalid.flash_profiles = flashes;
        invalid.flash_profile_count = ARRAY_SIZE(flashes);
        err = NULL;
        CHECK(!dm_mc02_board_validate(&invalid, &err));
        CHECK(err != NULL);
        error_free(err);

        flashes[0].ospi_index = 1;
        flashes[0].memory_mapped = true;
        invalid = *board;
        invalid.flash_profiles = flashes;
        invalid.flash_profile_count = 1;
        err = NULL;
        CHECK(!dm_mc02_board_validate(&invalid, &err));
        CHECK(err != NULL);
        error_free(err);
    }
}

int main(void)
{
    test_profile();
    test_gpio_snapshot();
    test_spi_cs_routes();
    test_rejected_inputs();
    puts("board profile smoke: PASS");
    return 0;
}
