/* DM-MC02 board wiring.  This is data only; chip models remain reusable. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_board.h"
#include "hw/arm/dm_mc02_adc.h"
#include "hw/arm/dm_mc02_dma.h"
#include "hw/arm/dm_mc02_exti.h"
#include "hw/arm/dm_mc02_fdcan.h"
#include "hw/arm/dm_mc02_rng.h"
#include "hw/arm/dm_mc02_usb.h"
#include "hw/arm/dm_mc02_wwdg.h"
#include "hw/arm/dm_mc02_tim2.h"
#include "hw/arm/dm_mc02_uart.h"
#include "hw/or-irq.h"
#include "hw/qdev-core.h"

static const DmMc02BoardDmaUartRoute dma1_uart_routes[] = {
    { 0, 0, false }, /* USART1 RX */
    { 1, 0, true },  /* USART1 TX */
    { 5, 3, false }, /* UART5 RX */
    { 6, 1, false }, /* USART2 RX */
    { 7, 2, false }, /* USART3 RX */
};

static const DmMc02BoardDmaUartRoute dma2_uart_routes[] = {
    { 0, 5, false }, /* USART10 RX */
    { 1, 1, true },  /* USART2 TX */
    { 2, 2, true },  /* USART3 TX */
    { 3, 5, true },  /* USART10 TX */
    { 4, 4, false }, /* UART7 RX */
    { 5, 4, true },  /* UART7 TX */
};

static const DmMc02BoardSpiCsRoute dm_mc02_spi_cs_routes[] = {
    { .controller = 2, .target_index = 0, .pin = { 2, 0 },
      .active_low = true }, /* BMI088 accelerometer */
    { .controller = 2, .target_index = 1, .pin = { 2, 3 },
      .active_low = true }, /* BMI088 gyroscope */
};

static const DmMc02BoardSpiCsRoute stm32h723_eval_spi_cs_routes[] = {
    { .controller = 2, .target_index = 0, .pin = { 2, 4 },
      .active_low = true }, /* BMI088 accelerometer */
    { .controller = 2, .target_index = 1, .pin = { 2, 5 },
      .active_low = true }, /* BMI088 gyroscope */
};

static const DmMc02BoardFlashProfile dm_mc02_flash_profiles[] = {
    {
        .ospi_index = 2,
        .name = "W25Q64JV",
        .storage_size = 8 * 1024 * 1024,
        .page_size = 256,
        .sector_size = 4 * 1024,
        .jedec_id = { 0xef, 0x40, 0x17 },
        .memory_mapped = true,
    },
};

static const DmMc02BoardProfile dm_mc02_profile = {
    .name = "DM-MC02",
    .soc = &dm_mc02_stm32h723_soc,
    .spi_cs_routes = dm_mc02_spi_cs_routes,
    .spi_cs_route_count = ARRAY_SIZE(dm_mc02_spi_cs_routes),
    .flash_profiles = dm_mc02_flash_profiles,
    .flash_profile_count = ARRAY_SIZE(dm_mc02_flash_profiles),
    .devices = {
        .led = { 0, 7 },
        .buzzer = { 1, 15 },
        .user_key = { 0, 15 },
        .power_out1 = { 2, 14 },
        .power_out2 = { 2, 13 },
        .power_5v = { 2, 15 },
        .bmi_spi_controller = 2,
        .bmi_spi_tx_dma = 0,
        .bmi_spi_rx_dma = 0,
        .bmi_spi_tx_stream = 4,
        .bmi_spi_rx_stream = 3,
        .bmi_spi_tx_request = 40,
        .bmi_spi_rx_request = 39,
        .tim1_index = 0,
        .tim8_index = 2,
        .buzzer_timer_index = 3,
        .buzzer_channel = 2,
        .buzzer_gpio_af = 2,
        .ws2812_dma_controller = 1,
        .ws2812_dma_stream = 6,
        .power_gpio_bank = 2,
        .power_vin_adc_channel = 4,
        .power_key_adc_channel = 19,
    },
    .tim2 = { .base = 0x40000000, .irq = 28,
              .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
              .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM2_TRGO,
              .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE },
    .timers = {
        { .base = 0x40010000, .irq = 25,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB2,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM1_TRGO,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_TIM1_TRGO2,
          .has_repetition_counter = true, .has_break_output = true,
          .has_complementary_output = true },
        { .base = 0x40000400, .irq = 29,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM3_TRGO,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE,
          .master_event_source_id = {
              [DM_MC02_TIM_TRGO][DM_MC02_TIM_MASTER_OC4REF] =
                  DM_MC02_TRIGGER_SOURCE_TIM3_CH4,
          },
          .master_event_source_valid = {
              [DM_MC02_TIM_TRGO] = 1u << DM_MC02_TIM_MASTER_OC4REF,
          } },
        { .base = 0x40010400, .irq = 44,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB2,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM8_TRGO,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_TIM8_TRGO2,
          .has_repetition_counter = true, .has_break_output = true,
          .has_complementary_output = true },
        { .base = 0x40001800, .irq = 43,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_NONE,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE },
        { .base = 0x4000e400, .irq = 162,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_NONE,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE },
    },
    .timer_count = DM_MC02_BOARD_TIMER_COUNT,
    .uarts = {
        { .name = "dm-mc02.usart1", .base = 0x40011000,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART16,
          .serial_slot = 1, .has_rx = true, .has_tx = true,
          .rx_request = 41, .tx_request = 42, .irq = 37 },
        { .name = "dm-mc02.usart2", .base = 0x40004400,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART234578,
          .serial_slot = 2, .has_rx = true, .has_tx = true,
          .tx_dma2 = true, .rx_request = 43, .tx_request = 44,
          .rs485 = true, .rs485_gpio_bank = 3, .rs485_gpio_pin = 4,
          .rs485_gpio_af = 7, .irq = 38 },
        { .name = "dm-mc02.usart3", .base = 0x40004800,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART234578,
          .serial_slot = 3, .has_rx = true, .has_tx = true,
          .tx_dma2 = true, .rx_request = 45, .tx_request = 46,
          .rs485 = true, .rs485_gpio_bank = 1, .rs485_gpio_pin = 14,
          .rs485_gpio_af = 7, .irq = 39 },
        { .name = "dm-mc02.uart5", .base = 0x40005000,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART234578,
          .serial_slot = 4, .has_rx = true, .rx_request = 65, .irq = 53 },
        { .name = "dm-mc02.uart7", .base = 0x40007800,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART234578,
          .serial_slot = 5, .has_rx = true, .has_tx = true,
          .rx_dma2 = true, .tx_dma2 = true, .rx_request = 79,
          .tx_request = 80, .irq = 82 },
        { .name = "dm-mc02.usart10", .base = 0x40011c00,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART16,
          .serial_slot = 6, .has_rx = true, .has_tx = true,
          .rx_dma2 = true, .tx_dma2 = true, .rx_request = 118,
          .tx_request = 119, .irq = 156 },
    },
    .uart_count = DM_MC02_BOARD_UART_COUNT,
    .fdcans = {
        { .name = "dm-mc02.fdcan1", .base = 0x4000a000,
          .region_size = 0x400, .serial_slot = 7, .irq = { 19, 21 } },
        { .name = "dm-mc02.fdcan2", .base = 0x4000a400,
          .region_size = 0x800, .serial_slot = 8, .irq = { 20, 22 } },
        { .name = "dm-mc02.fdcan3", .base = 0x4000d400,
          .region_size = 0x800, .serial_slot = 9, .irq = { 159, 160 } },
    },
    .fdcan_count = DM_MC02_BOARD_FDCAN_COUNT,
    .adc1_dr = 0x40022040,
    .adc1_request = 9,
    .tim8_ccr1 = 0x40010434,
    .tim8_request = 47,
    .dma1_uart_routes = dma1_uart_routes,
    .dma1_uart_route_count = ARRAY_SIZE(dma1_uart_routes),
    .dma2_uart_routes = dma2_uart_routes,
    .dma2_uart_route_count = ARRAY_SIZE(dma2_uart_routes),
    .irqs = {
        .exti = { 6, 7, 8, 9, 10, 23, 40 },
        .adc = 18,
        .rng = 80,
        .usb = 77,
        .wwdg = 0,
        .dma1 = { 11, 12, 13, 14, 15, 16, 17, 47 },
        .dma2 = { 56, 57, 58, 59, 60, 68, 69, 70 },
    },
};

/* A deliberately small second wiring profile used to prove that the H723
 * SoC and reusable peripheral models are not tied to DM-MC02 pin choices.
 * It represents a generic evaluation carrier, not a claim about a specific
 * commercial board. */
static const DmMc02BoardDmaUartRoute stm32h723_eval_dma1_uart_routes[] = {
    { 0, 0, false }, /* USART1 RX */
    { 1, 0, true },  /* USART1 TX */
};

static const DmMc02BoardProfile stm32h723_eval_profile = {
    .name = "STM32H723-EVAL",
    .soc = &dm_mc02_stm32h723_soc,
    .spi_cs_routes = stm32h723_eval_spi_cs_routes,
    .spi_cs_route_count = ARRAY_SIZE(stm32h723_eval_spi_cs_routes),
    .devices = {
        .led = { 0, 0 },
        .buzzer = { 1, 0 },
        .user_key = { 0, 13 },
        .power_out1 = { 2, 0 },
        .power_out2 = { 2, 1 },
        .power_5v = { 2, 2 },
        .bmi_spi_controller = 2,
        .bmi_spi_tx_dma = 0,
        .bmi_spi_rx_dma = 0,
        .bmi_spi_tx_stream = 0,
        .bmi_spi_rx_stream = 1,
        .bmi_spi_tx_request = 40,
        .bmi_spi_rx_request = 39,
        .tim1_index = 0,
        .tim8_index = 2,
        .buzzer_timer_index = 3,
        .buzzer_channel = 1,
        .buzzer_gpio_af = 1,
        .ws2812_dma_controller = 1,
        .ws2812_dma_stream = 0,
        .power_gpio_bank = 2,
        .power_vin_adc_channel = 4,
        .power_key_adc_channel = 19,
    },
    .tim2 = { .base = 0x40000000, .irq = 28,
              .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
              .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM2_TRGO,
              .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE },
    .timers = {
        { .base = 0x40010000, .irq = 25,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB2,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM1_TRGO,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_TIM1_TRGO2,
          .has_repetition_counter = true, .has_complementary_output = true },
        { .base = 0x40000400, .irq = 29,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM3_TRGO,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE,
          .master_event_source_id = {
              [DM_MC02_TIM_TRGO][DM_MC02_TIM_MASTER_OC4REF] =
                  DM_MC02_TRIGGER_SOURCE_TIM3_CH4,
          },
          .master_event_source_valid = {
              [DM_MC02_TIM_TRGO] = 1u << DM_MC02_TIM_MASTER_OC4REF,
          } },
        { .base = 0x40010400, .irq = 44,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB2,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_TIM8_TRGO,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_TIM8_TRGO2,
          .has_repetition_counter = true, .has_complementary_output = true },
        { .base = 0x40001800, .irq = 43,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_NONE,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE },
        { .base = 0x4000e400, .irq = 162,
          .clock_domain = DM_MC02_BOARD_TIMER_CLOCK_APB1,
          .trgo_source_id = DM_MC02_TRIGGER_SOURCE_NONE,
          .trgo2_source_id = DM_MC02_TRIGGER_SOURCE_NONE },
    },
    .timer_count = DM_MC02_BOARD_TIMER_COUNT,
    .uarts = {
        { .name = "stm32h723-eval.usart1", .base = 0x40011000,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART16,
          .serial_slot = 1, .has_rx = true, .has_tx = true,
          .rx_request = 41, .tx_request = 42, .irq = 37 },
        { .name = "stm32h723-eval.usart2", .base = 0x40004400,
          .kernel_clock_group = DM_MC02_UART_KERNEL_CLOCK_USART234578,
          .serial_slot = 2, .has_rx = true, .has_tx = true,
          .rx_request = 43, .tx_request = 44, .irq = 38 },
    },
    .uart_count = 2,
    .fdcans = {
        { .name = "stm32h723-eval.fdcan1", .base = 0x4000a000,
          .region_size = 0x400, .serial_slot = 7, .irq = { 19, 21 } },
    },
    .fdcan_count = 1,
    .adc1_dr = 0x40022040,
    .adc1_request = 9,
    .tim8_ccr1 = 0x40010434,
    .tim8_request = 47,
    .dma1_uart_routes = stm32h723_eval_dma1_uart_routes,
    .dma1_uart_route_count = ARRAY_SIZE(stm32h723_eval_dma1_uart_routes),
    .irqs = {
        .exti = { 6, 7, 8, 9, 10, 23, 40 },
        .adc = 18,
        .rng = 80,
        .usb = 77,
        .wwdg = 0,
        .dma1 = { 11, 12, 13, 14, 15, 16, 17, 47 },
        .dma2 = { 56, 57, 58, 59, 60, 68, 69, 70 },
    },
};

static const DmMc02BoardProfile * const dm_mc02_profiles[] = {
    &dm_mc02_profile,
    &stm32h723_eval_profile,
};

const DmMc02BoardProfile *dm_mc02_board_dm_mc02(void)
{
    return &dm_mc02_profile;
}

const DmMc02BoardProfile *dm_mc02_board_lookup(const char *name)
{
    for (size_t i = 0; i < ARRAY_SIZE(dm_mc02_profiles); ++i) {
        if (!name || !name[0] ||
            g_str_equal(name, dm_mc02_profiles[i]->name)) {
            return dm_mc02_profiles[i];
        }
    }
    return NULL;
}

static bool dm_mc02_board_pin_valid(const DmMc02BoardProfile *board,
                                    DmMc02BoardPin pin)
{
    return board && board->soc && pin.bank < board->soc->gpio_bank_count &&
           pin.pin < 16;
}

static bool dm_mc02_board_flash_profile_valid(
    const DmMc02BoardProfile *board,
    const DmMc02BoardFlashProfile *flash)
{
    bool nonzero_id = false;

    if (!flash || (flash->ospi_index != 1 && flash->ospi_index != 2) ||
        !flash->name || !flash->name[0] || !flash->storage_size ||
        !flash->page_size || !flash->sector_size ||
        flash->sector_size < flash->page_size ||
        flash->storage_size % flash->page_size ||
        flash->storage_size % flash->sector_size ||
        flash->page_size > DM_MC02_BOARD_FLASH_MAX_PAGE_SIZE) {
        return false;
    }
    for (unsigned i = 0; i < ARRAY_SIZE(flash->jedec_id); ++i) {
        nonzero_id |= flash->jedec_id[i] != 0;
    }
    if (!nonzero_id) {
        return false;
    }
    return !flash->memory_mapped ||
           (flash->ospi_index == 2 && board->soc->ospi2_memory_base);
}

static unsigned dm_mc02_board_gpio_mode(const DmMc02BoardGpioState *gpio,
                                        unsigned pin)
{
    return (gpio->moder >> (pin * 2)) & 3u;
}

static unsigned dm_mc02_board_gpio_af(const DmMc02BoardGpioState *gpio,
                                      unsigned pin)
{
    uint32_t afr = pin < 8 ? gpio->afr0 : gpio->afr1;
    unsigned shift = (pin & 7u) * 4u;

    return (afr >> shift) & 0xfu;
}

static bool dm_mc02_board_gpio_high(const DmMc02BoardProfile *board,
                                    const DmMc02BoardGpioState *gpio,
                                    DmMc02BoardPin pin)
{
    return dm_mc02_board_pin_valid(board, pin) &&
           (gpio[pin.bank].odr & (UINT32_C(1) << pin.pin));
}

bool dm_mc02_board_decode_gpio_outputs(
    const DmMc02BoardProfile *board,
    const DmMc02BoardGpioState *gpio,
    size_t gpio_count,
    unsigned changed_bank,
    DmMc02BoardSignals *signals)
{
    const DmMc02BoardDeviceWiring *devices;

    if (!board || !gpio || !signals || !board->soc ||
        gpio_count < board->soc->gpio_bank_count ||
        board->soc->gpio_bank_count > DM_MC02_BOARD_GPIO_BANK_COUNT ||
        board->uart_count > DM_MC02_BOARD_UART_COUNT ||
        board->devices.power_gpio_bank >= gpio_count) {
        return false;
    }
    memset(signals, 0, sizeof(*signals));
    devices = &board->devices;
    signals->power_gpio_odr = gpio[devices->power_gpio_bank].odr;
    signals->led_high = dm_mc02_board_gpio_high(board, gpio, devices->led);
    signals->led_dirty = changed_bank == devices->led.bank;

    if (dm_mc02_board_pin_valid(board, devices->buzzer)) {
        const DmMc02BoardGpioState *buzzer = &gpio[devices->buzzer.bank];
        unsigned mode = dm_mc02_board_gpio_mode(buzzer,
                                                 devices->buzzer.pin);

        signals->buzzer_gpio_output = mode == 1;
        signals->buzzer_gpio_high =
            signals->buzzer_gpio_output &&
            (buzzer->odr & (UINT32_C(1) << devices->buzzer.pin));
        signals->buzzer_af_active =
            mode == 2 &&
            dm_mc02_board_gpio_af(buzzer, devices->buzzer.pin) ==
                devices->buzzer_gpio_af;
    }

    for (size_t i = 0; i < board->uart_count; ++i) {
        const DmMc02BoardUartRoute *route = &board->uarts[i];
        DmMc02Rs485Signal *signal = &signals->rs485[i];

        if (!route->rs485 ||
            !dm_mc02_board_pin_valid(
                board, (DmMc02BoardPin){ route->rs485_gpio_bank,
                                         route->rs485_gpio_pin })) {
            continue;
        }
        signal->level = (gpio[route->rs485_gpio_bank].odr &
                         (UINT32_C(1) << route->rs485_gpio_pin)) != 0;
        switch (dm_mc02_board_gpio_mode(&gpio[route->rs485_gpio_bank],
                                         route->rs485_gpio_pin)) {
        case 1:
            signal->mode = DM_MC02_RS485_DE_MANUAL_GPIO;
            break;
        case 2:
            if (dm_mc02_board_gpio_af(&gpio[route->rs485_gpio_bank],
                                      route->rs485_gpio_pin) ==
                route->rs485_gpio_af) {
                signal->mode = DM_MC02_RS485_DE_AUTO_USART;
            }
            break;
        default:
            break;
        }
    }
    return true;
}

bool dm_mc02_board_decode_spi_selected_mask(
    const DmMc02BoardProfile *board,
    const DmMc02BoardGpioState *gpio,
    size_t gpio_count,
    unsigned controller,
    size_t target_capacity,
    uint32_t *selected_mask)
{
    if (!board || !gpio || !selected_mask || !board->soc || !controller ||
        !target_capacity || target_capacity > 32 ||
        (board->spi_cs_route_count && !board->spi_cs_routes) ||
        gpio_count < board->soc->gpio_bank_count) {
        return false;
    }

    *selected_mask = 0;
    for (size_t i = 0; i < board->spi_cs_route_count; ++i) {
        const DmMc02BoardSpiCsRoute *route = &board->spi_cs_routes[i];
        const DmMc02BoardGpioState *bank;
        bool high;

        if (route->controller != controller) {
            continue;
        }
        if (route->target_index >= target_capacity ||
            !dm_mc02_board_pin_valid(board, route->pin)) {
            return false;
        }
        bank = &gpio[route->pin.bank];
        if (dm_mc02_board_gpio_mode(bank, route->pin.pin) != 1) {
            continue;
        }
        high = (bank->odr & (UINT32_C(1) << route->pin.pin)) != 0;
        if (route->active_low ? !high : high) {
            *selected_mask |= UINT32_C(1) << route->target_index;
        }
    }
    return true;
}

static bool dm_mc02_board_validate_pin(const DmMc02BoardProfile *board,
                                       DmMc02BoardPin pin, const char *name,
                                       Error **errp)
{
    if (dm_mc02_board_pin_valid(board, pin)) {
        return true;
    }
    error_setg(errp, "board profile '%s' has invalid %s pin",
               board && board->name ? board->name : "<unnamed>", name);
    return false;
}

static bool dm_mc02_board_validate_irq(const DmMc02BoardProfile *board,
                                       unsigned irq, const char *name,
                                       Error **errp)
{
    if (board->soc && irq < board->soc->irq_count) {
        return true;
    }
    error_setg(errp, "board profile '%s' has invalid %s IRQ %u",
               board && board->name ? board->name : "<unnamed>", name, irq);
    return false;
}

static bool dm_mc02_board_validate_timer_route(
    const DmMc02BoardProfile *board, const DmMc02BoardTimerRoute *route,
    size_t index, hwaddr expected_base, const char *name, Error **errp)
{
    if (!route || !route->base ||
        route->clock_domain >= DM_MC02_BOARD_TIMER_CLOCK_DOMAIN_COUNT ||
        (expected_base && route->base != expected_base)) {
        error_setg(errp, "board profile '%s' has invalid %s timer route %zu",
                   board->name, name, index);
        return false;
    }
    if (!dm_mc02_board_validate_irq(board, route->irq, name, errp)) {
        return false;
    }
    if ((route->trgo_source_id != DM_MC02_TRIGGER_SOURCE_NONE &&
         route->trgo_source_id > 0x1f) ||
        (route->trgo2_source_id != DM_MC02_TRIGGER_SOURCE_NONE &&
         route->trgo2_source_id > 0x1f)) {
        error_setg(errp,
                   "board profile '%s' has invalid %s trigger source %zu",
                   board->name, name, index);
        return false;
    }
    for (unsigned output = 0; output < 2; ++output) {
        for (unsigned kind = 0;
             kind < DM_MC02_BOARD_TIMER_EVENT_KIND_COUNT; ++kind) {
            if ((route->master_event_source_valid[output] & (1u << kind)) &&
                route->master_event_source_id[output][kind] > 0x1f) {
                error_setg(errp,
                           "board profile '%s' has invalid %s timer event source %zu/%u",
                           board->name, name, index, kind);
                return false;
            }
        }
    }
    return true;
}

bool dm_mc02_board_validate(const DmMc02BoardProfile *board, Error **errp)
{
    bool serial_used[11] = { [0] = true, [10] = true };
    bool dma1_stream_used[DM_MC02_BOARD_DMA_STREAM_COUNT] = { 0 };
    bool dma2_stream_used[DM_MC02_BOARD_DMA_STREAM_COUNT] = { 0 };
    bool bmi_target_seen[2] = { false, false };

    if (!board || !board->name || !board->name[0] || !board->soc) {
        error_setg(errp, "board profile is incomplete");
        return false;
    }
    if (!board->soc->gpio_bank_count ||
        board->soc->gpio_bank_count > DM_MC02_BOARD_GPIO_BANK_COUNT) {
        error_setg(errp, "board profile '%s' requires %u GPIO banks, maximum is %u",
                   board->name, board->soc->gpio_bank_count,
                   DM_MC02_BOARD_GPIO_BANK_COUNT);
        return false;
    }
    if (!board->soc->irq_count || !board->soc->reset_cpu_hz ||
        !board->soc->refclk_hz) {
        error_setg(errp, "board profile '%s' has an invalid clock or IRQ configuration",
                   board->name);
        return false;
    }
    if (!dm_mc02_soc_validate_map(board->soc)) {
        error_setg(errp, "board profile '%s' references an invalid SoC map",
                   board->name);
        return false;
    }
    if (board->timer_count > DM_MC02_BOARD_TIMER_COUNT ||
        board->uart_count > DM_MC02_BOARD_UART_COUNT ||
        board->fdcan_count > DM_MC02_BOARD_FDCAN_COUNT) {
        error_setg(errp, "board profile '%s' exceeds machine device capacity",
                   board->name);
        return false;
    }
    if (!board->timer_count ||
        (board->devices.tim1_index != UINT_MAX &&
         board->devices.tim1_index >= board->timer_count) ||
        board->devices.tim8_index >= board->timer_count ||
        board->devices.buzzer_timer_index >= board->timer_count ||
        board->devices.buzzer_channel < 1 ||
        board->devices.buzzer_channel > 4) {
        error_setg(errp, "board profile '%s' has an invalid timer wiring",
                   board->name);
        return false;
    }
    if (board->devices.buzzer_gpio_af > 15) {
        error_setg(errp, "board profile '%s' has an invalid buzzer GPIO AF",
                   board->name);
        return false;
    }
    if (board->devices.ws2812_dma_controller > 1 ||
        board->devices.ws2812_dma_stream >= DM_MC02_BOARD_DMA_STREAM_COUNT) {
        error_setg(errp, "board profile '%s' has invalid WS2812 DMA wiring",
                   board->name);
        return false;
    }
    if (board->devices.bmi_spi_controller != 2 ||
        board->devices.bmi_spi_tx_dma > 1 ||
        board->devices.bmi_spi_rx_dma > 1 ||
        board->devices.bmi_spi_tx_stream >= DM_MC02_BOARD_DMA_STREAM_COUNT ||
        board->devices.bmi_spi_rx_stream >= DM_MC02_BOARD_DMA_STREAM_COUNT ||
        board->devices.bmi_spi_tx_request > 0x7f ||
        board->devices.bmi_spi_rx_request > 0x7f) {
        error_setg(errp, "board profile '%s' has unsupported BMI088 SPI/DMA wiring",
                   board->name);
        return false;
    }
    if ((board->spi_cs_route_count && !board->spi_cs_routes) ||
        !board->spi_cs_route_count) {
        error_setg(errp, "board profile '%s' has no SPI CS route table",
                   board->name);
        return false;
    }
    if (board->flash_profile_count > DM_MC02_BOARD_OSPI_COUNT ||
        (board->flash_profile_count && !board->flash_profiles)) {
        error_setg(errp, "board profile '%s' has invalid Flash profile table",
                   board->name);
        return false;
    }
    for (size_t i = 0; i < board->flash_profile_count; ++i) {
        const DmMc02BoardFlashProfile *flash = &board->flash_profiles[i];

        if (!dm_mc02_board_flash_profile_valid(board, flash)) {
            error_setg(errp,
                       "board profile '%s' has invalid Flash profile %zu",
                       board->name, i);
            return false;
        }
        for (size_t j = 0; j < i; ++j) {
            if (board->flash_profiles[j].ospi_index == flash->ospi_index) {
                error_setg(errp,
                           "board profile '%s' duplicates OSPI Flash %u",
                           board->name, flash->ospi_index);
                return false;
            }
        }
    }
    for (size_t i = 0; i < board->spi_cs_route_count; ++i) {
        const DmMc02BoardSpiCsRoute *route = &board->spi_cs_routes[i];

        if (!route->controller || route->target_index >= 32 ||
            !dm_mc02_board_validate_pin(board, route->pin, "SPI CS", errp)) {
            return false;
        }
        for (size_t j = 0; j < i; ++j) {
            const DmMc02BoardSpiCsRoute *previous =
                &board->spi_cs_routes[j];

            if (previous->controller == route->controller &&
                previous->target_index == route->target_index) {
                error_setg(errp,
                           "board profile '%s' duplicates SPI target route %zu",
                           board->name, i);
                return false;
            }
        }
        if (route->controller == board->devices.bmi_spi_controller &&
            route->target_index < ARRAY_SIZE(bmi_target_seen)) {
            bmi_target_seen[route->target_index] = true;
        }
    }
    if (!bmi_target_seen[0] || !bmi_target_seen[1]) {
        error_setg(errp,
                   "board profile '%s' must route BMI088 SPI targets 0 and 1",
                   board->name);
        return false;
    }
    if (board->devices.power_gpio_bank >= board->soc->gpio_bank_count ||
        board->devices.power_vin_adc_channel >= 32 ||
        board->devices.power_key_adc_channel >= 32) {
        error_setg(errp, "board profile '%s' has invalid power wiring",
                   board->name);
        return false;
    }
    if (!dm_mc02_board_validate_pin(board, board->devices.led, "LED", errp) ||
        !dm_mc02_board_validate_pin(board, board->devices.buzzer, "buzzer", errp) ||
        !dm_mc02_board_validate_pin(board, board->devices.user_key, "user-key", errp) ||
        !dm_mc02_board_validate_pin(board, board->devices.power_out1, "power-out1", errp) ||
        !dm_mc02_board_validate_pin(board, board->devices.power_out2, "power-out2", errp) ||
        !dm_mc02_board_validate_pin(board, board->devices.power_5v, "5V", errp)) {
        return false;
    }

    if (!dm_mc02_board_validate_irq(board, board->irqs.adc, "ADC", errp) ||
        !dm_mc02_board_validate_irq(board, board->irqs.rng, "RNG", errp) ||
        !dm_mc02_board_validate_irq(board, board->irqs.usb, "USB", errp) ||
        !dm_mc02_board_validate_irq(board, board->irqs.wwdg, "WWDG", errp) ||
        !dm_mc02_board_validate_timer_route(
            board, &board->tim2, 0, board->soc->tim2_base, "TIM2", errp)) {
        return false;
    }
    for (size_t i = 0; i < board->timer_count; ++i) {
        if (!dm_mc02_board_validate_timer_route(
                board, &board->timers[i], i, 0, "auxiliary", errp)) {
            return false;
        }
    }

    for (size_t i = 0; i < board->uart_count; ++i) {
        const DmMc02BoardUartRoute *route = &board->uarts[i];

        if (!route->name || !route->name[0] || !route->base ||
            route->kernel_clock_group >=
                DM_MC02_UART_KERNEL_CLOCK_GROUP_COUNT ||
            route->serial_slot >= ARRAY_SIZE(serial_used) ||
            serial_used[route->serial_slot]) {
            error_setg(errp, "board profile '%s' has invalid UART route %zu",
                       board->name, i);
            return false;
        }
        if (!dm_mc02_board_validate_irq(board, route->irq, "UART", errp)) {
            return false;
        }
        serial_used[route->serial_slot] = true;
        if (route->rx_request > 0x7f || route->tx_request > 0x7f ||
            (route->rs485 && route->rs485_gpio_af > 15)) {
            error_setg(errp, "board profile '%s' has invalid UART DMA/RS485 route %zu",
                       board->name, i);
            return false;
        }
        if (route->rs485 &&
            !dm_mc02_board_validate_pin(
                board, (DmMc02BoardPin){ route->rs485_gpio_bank,
                                         route->rs485_gpio_pin },
                "RS485 DE", errp)) {
            return false;
        }
    }

    for (size_t i = 0; i < board->fdcan_count; ++i) {
        const DmMc02BoardFdcanRoute *route = &board->fdcans[i];

        if (!route->name || !route->name[0] || !route->base ||
            !route->region_size ||
            route->serial_slot >= ARRAY_SIZE(serial_used) ||
            serial_used[route->serial_slot]) {
            error_setg(errp, "board profile '%s' has invalid FDCAN route %zu",
                       board->name, i);
            return false;
        }
        if (!dm_mc02_board_validate_irq(board, route->irq[0], "FDCAN", errp) ||
            !dm_mc02_board_validate_irq(board, route->irq[1], "FDCAN", errp)) {
            return false;
        }
        serial_used[route->serial_slot] = true;
    }

    for (unsigned i = 0; i < DM_MC02_BOARD_EXTI_ROUTE_COUNT; ++i) {
        if (!dm_mc02_board_validate_irq(board, board->irqs.exti[i],
                                        "EXTI", errp)) {
            return false;
        }
    }
    for (unsigned i = 0; i < DM_MC02_BOARD_DMA_STREAM_COUNT; ++i) {
        if (!dm_mc02_board_validate_irq(board, board->irqs.dma1[i],
                                        "DMA1", errp) ||
            !dm_mc02_board_validate_irq(board, board->irqs.dma2[i],
                                        "DMA2", errp)) {
            return false;
        }
    }

    if ((board->dma1_uart_route_count && !board->dma1_uart_routes) ||
        (board->dma2_uart_route_count && !board->dma2_uart_routes)) {
        error_setg(errp, "board profile '%s' has a missing DMA UART route table",
                   board->name);
        return false;
    }
    for (size_t i = 0; i < board->dma1_uart_route_count; ++i) {
        const DmMc02BoardDmaUartRoute *route = &board->dma1_uart_routes[i];

        if (route->stream >= DM_MC02_BOARD_DMA_STREAM_COUNT ||
            route->uart_index >= board->uart_count ||
            dma1_stream_used[route->stream]) {
            error_setg(errp, "board profile '%s' has invalid DMA1 UART route %zu",
                       board->name, i);
            return false;
        }
        dma1_stream_used[route->stream] = true;
    }
    for (size_t i = 0; i < board->dma2_uart_route_count; ++i) {
        const DmMc02BoardDmaUartRoute *route = &board->dma2_uart_routes[i];

        if (route->stream >= DM_MC02_BOARD_DMA_STREAM_COUNT ||
            route->uart_index >= board->uart_count ||
            dma2_stream_used[route->stream]) {
            error_setg(errp, "board profile '%s' has invalid DMA2 UART route %zu",
                       board->name, i);
            return false;
        }
        dma2_stream_used[route->stream] = true;
    }

    return true;
}

void dm_mc02_board_connect_irqs(const DmMc02BoardProfile *board,
                                Object *owner, DeviceState *armv7m,
                                DmMc02Exti *exti, DmMc02Adc *adc1,
                                DmMc02Adc *adc2, DeviceState **adc_irq_or,
                                DmMc02Tim2 *tim2, DmMc02Tim2 *tim_aux,
                                DmMc02Fdcan *fdcan, DmMc02Uart *uart,
                                DmMc02Rng *rng,
                                DmMc02Dma *dma1, DmMc02Dma *dma2,
                                DmMc02Usb *usb, DmMc02Wwdg *wwdg,
                                Error **errp)
{
    DmMc02ExtiIrqRoute exti_routes[DM_MC02_EXTI_IRQ_GROUP_COUNT];

    for (unsigned line = 0; line < DM_MC02_EXTI_IRQ_GROUP_COUNT; ++line) {
        exti_routes[line] = (DmMc02ExtiIrqRoute) {
            .exti_group = line,
            .controller_input = board->irqs.exti[line],
            .controller_irq = qdev_get_gpio_in(
                armv7m, board->irqs.exti[line]),
        };
    }
    if (!dm_mc02_exti_connect_nvic(exti, exti_routes,
                                   ARRAY_SIZE(exti_routes),
                                   board->soc->irq_count, errp)) {
        return;
    }

    /* ADC1 and ADC2 share the board-selected ADC global vector. */
    *adc_irq_or = DEVICE(object_new(TYPE_OR_IRQ));
    object_property_add_child(owner, "adc-irq-or", OBJECT(*adc_irq_or));
    object_property_set_int(OBJECT(*adc_irq_or), "num-lines", 2,
                            &error_fatal);
    qdev_realize_and_unref(*adc_irq_or, NULL, errp);
    qdev_connect_gpio_out(*adc_irq_or, 0,
                          qdev_get_gpio_in(armv7m, board->irqs.adc));
    dm_mc02_adc_set_irq(adc1, qdev_get_gpio_in(*adc_irq_or, 0));
    dm_mc02_adc_set_irq(adc2, qdev_get_gpio_in(*adc_irq_or, 1));

    dm_mc02_rng_set_irq(rng, qdev_get_gpio_in(armv7m, board->irqs.rng));

    dm_mc02_usb_set_irq(usb, qdev_get_gpio_in(armv7m, board->irqs.usb));
    dm_mc02_wwdg_set_irq(wwdg, qdev_get_gpio_in(armv7m, board->irqs.wwdg));

    dm_mc02_tim2_set_irq(tim2,
                         qdev_get_gpio_in(armv7m, board->tim2.irq));
    for (size_t i = 0; i < board->timer_count; ++i) {
        dm_mc02_tim2_set_irq(&tim_aux[i],
                             qdev_get_gpio_in(armv7m, board->timers[i].irq));
    }

    for (size_t i = 0; i < board->fdcan_count; ++i) {
        for (unsigned line = 0; line < 2; ++line) {
            dm_mc02_fdcan_set_irq(&fdcan[i], line,
                                  qdev_get_gpio_in(
                                      armv7m, board->fdcans[i].irq[line]));
        }
    }

    for (size_t i = 0; i < board->uart_count; ++i) {
        dm_mc02_uart_set_irq(&uart[i],
                             qdev_get_gpio_in(armv7m, board->uarts[i].irq));
    }

    for (unsigned stream = 0; stream < DM_MC02_BOARD_DMA_STREAM_COUNT;
         ++stream) {
        dm_mc02_dma_set_stream_irq(
            dma1, stream, qdev_get_gpio_in(armv7m, board->irqs.dma1[stream]));
        dm_mc02_dma_set_stream_irq(
            dma2, stream, qdev_get_gpio_in(armv7m, board->irqs.dma2[stream]));
    }
}
