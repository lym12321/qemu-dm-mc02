/*
 * DM-MC02 minimal board model.
 *
 * M1/M2 model the Cortex-M7 execution core, the STM32H723 memory map, the
 * polled subset of SPI2 needed to exercise the two BMI088 dies, and the
 * board-specific DMA/WS2812 observation path. The rest of the H723 peripheral
 * matrix remains separate milestones.
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/error-report.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "exec/address-spaces.h"
#include "sysemu/dma.h"
#include "hw/arm/armv7m.h"
#include "hw/arm/boot.h"
#include "hw/irq.h"
#include "hw/arm/dm_mc02_gpio.h"
#include "hw/arm/dm_mc02_gpio_exti.h"
#include "hw/arm/dm_mc02_board.h"
#include "hw/arm/dm_mc02_board_reset.h"
#include "hw/arm/dm_mc02_cosim_link.h"
#include "hw/arm/dm_mc02_flash.h"
#include "hw/arm/dm_mc02_fmc.h"
#include "hw/arm/dm_mc02_tim2.h"
#include "hw/arm/dm_mc02_syscfg.h"
#include "hw/arm/dm_mc02_exti.h"
#include "hw/arm/dm_mc02_pwr_rcc.h"
#include "hw/arm/dm_mc02_reset_input.h"
#include "hw/arm/dm_mc02_uart.h"
#include "hw/arm/dm_mc02_fdcan.h"
#include "hw/arm/dm_mc02_adc.h"
#include "hw/arm/dm_mc02_adc_common.h"
#include "hw/arm/dm_mc02_power.h"
#include "hw/arm/dm_mc02_dma.h"
#include "hw/arm/dm_mc02_dbgmcu.h"
#include "hw/arm/dm_mc02_rng.h"
#include "hw/arm/dm_mc02_crc.h"
#include "hw/arm/dm_mc02_ospi.h"
#include "hw/arm/dm_mc02_iwdg.h"
#include "hw/arm/dm_mc02_wwdg.h"
#include "hw/arm/dm_mc02_usb.h"
#include "hw/arm/dm_mc02_cordic.h"
#include "hw/arm/dm_mc02_bmi088.h"
#include "hw/arm/dm_mc02_bmi088_spi.h"
#include "hw/arm/dm_mc02_bmi088_spi_link.h"
#include "hw/arm/dm_mc02_spi.h"
#include "hw/arm/dm_mc02_trigger.h"
#include "hw/core/cpu.h"
#include "hw/boards.h"
#include "chardev/char.h"
#include "hw/qdev-clock.h"
#include "hw/qdev-properties.h"
#include "sysemu/sysemu.h"
#include "sysemu/reset.h"
#include "sysemu/runstate.h"
#include "net/can_emu.h"

#include <math.h>

#define DM_MC02_GPIO_BANKS   DM_MC02_BOARD_GPIO_BANK_COUNT
#define DM_MC02_TIM8_DIER_CC1DE (1u << 9)
#define DM_MC02_DMA_CR_DIR_P2M    0u
#define DM_MC02_DMA_CR_DIR_M2P    (1u << 6)
#define DM_MC02_DMA_CR_DIR_M2M    (2u << 6)

#define DM_MC02_SPI_TXDR          0x20
#define DM_MC02_SPI_RXDR          0x30

#define DM_MC02_USART_RDR         0x24
#define DM_MC02_USART_TDR         0x28

#define DM_MC02_USART1_RX_REQUEST  41
#define DM_MC02_USART1_TX_REQUEST  42
#define DM_MC02_USART2_RX_REQUEST  43
#define DM_MC02_USART2_TX_REQUEST  44
#define DM_MC02_USART3_RX_REQUEST  45
#define DM_MC02_USART3_TX_REQUEST  46
#define DM_MC02_UART5_RX_REQUEST   65
#define DM_MC02_UART7_RX_REQUEST   79
#define DM_MC02_UART7_TX_REQUEST   80
#define DM_MC02_USART10_RX_REQUEST 118
#define DM_MC02_USART10_TX_REQUEST 119

#define DM_MC02_DMA_CR_EN         (1u << 0)
#define DM_MC02_DMA_CR_MINC       (1u << 10)
#define DM_MC02_DMA_CR_PSIZE_SHIFT 11
#define DM_MC02_DMA_CR_MSIZE_SHIFT 13
#define DM_MC02_DMA_SIZE_16       1u

#define DM_MC02_WS2812_CHANNELS   2
#define DM_MC02_WS2812_BITS       24
#define DM_MC02_WS2812_FIRST_BITS 24
#define DM_MC02_WS2812_TOTAL_BITS 72
#define DM_MC02_WS2812_HIGH       168
#define DM_MC02_WS2812_LOW        84
#define DM_MC02_WS2812_APPROX_BATCH (DM_MC02_WS2812_TOTAL_BITS * 100)
#define DM_MC02_WS2812_THRESHOLD  ((DM_MC02_WS2812_HIGH + \
                                   DM_MC02_WS2812_LOW) / 2)

typedef struct DmMc02MachineState DmMc02MachineState;

typedef struct DmMc02TimerEventContext {
    DmMc02MachineState *machine;
    const DmMc02BoardTimerRoute *route;
} DmMc02TimerEventContext;

static void dm_mc02_adc_common_dma_stream_enabled(DmMc02MachineState *s);

struct DmMc02MachineState {
    MachineState parent_obj;

    const DmMc02BoardProfile *board;
    DmMc02SocMemory soc_memory;
    DmMc02Flash flash_regs;
    DmMc02Fmc fmc;
    DmMc02Tim2 tim2;
    DmMc02TimerEventContext tim2_event_context;
    DmMc02Tim2 tim_aux[DM_MC02_BOARD_TIMER_COUNT];
    DmMc02TimerEventContext timer_event_context[
        DM_MC02_BOARD_TIMER_COUNT];
    size_t initialized_timer_count;
    DmMc02GpioExti gpio_exti;
    DmMc02Uart uart[6];
    size_t initialized_uart_count;
    DmMc02Fdcan fdcan[3];
    size_t initialized_fdcan_count;
    CanBusState *canbus;
    DmMc02TriggerBus trigger_bus;
    DmMc02Adc adc[2];
    DmMc02AdcCommon adc12_common;
    DeviceState *adc_irq_or;
    DmMc02DmaSubsystem dma_subsystem;
    DmMc02Dbgmcu dbgmcu;
    DmMc02Rng rng;
    DmMc02Crc crc;
    DmMc02Ospi ospi2;
    DmMc02Ospi ospi1;
    DmMc02Ospim ospim;
    DmMc02Iwdg iwdg1;
    DmMc02Wwdg wwdg1;
    DmMc02Usb usb_hs;
    DmMc02Cordic cordic;
    DmMc02Power power;
    bool electrical_power;
    bool mcu_powered;
    bool cold_reset;
    uint32_t vin_mv;
    uint32_t iwdg_boot_grace_ms;
    uint32_t iwdg_lsi_hz;
    int32_t iwdg_lsi_error_ppm;
    DmMc02PwrRcc pwr_rcc;
    DmMc02ResetInput reset_input;
    qemu_irq sysresetreq_irq;
    DmMc02Bmi088SpiLink bmi_spi_link;
    DmMc02Spi spi1_state;
    uint64_t imu_consume_step_id;
    bool imu_consume_step_valid;
    QEMUTimer *ws2812_timer;
    uint32_t ws2812_led_rgb[DM_MC02_WS2812_CHANNELS];
    bool ws2812_dirty;
    Clock *sysclk;
    Clock *apb1_timerclk;
    Clock *apb2_timerclk;
    Clock *uart16_kernelclk;
    Clock *uart234578_kernelclk;
    Clock *uart_kernelclk[DM_MC02_BOARD_UART_COUNT];
    Clock *refclk;
    DmMc02CosimLink cosim;
    bool cosim_motor_loopback;
    uint64_t cosim_motor_loopback_steps;
    uint32_t imu_seed;
    bool accurate_timing;
    bool fdcan_host_ack;
    bool adc_accurate_timing;
    bool adc_power_model;
    bool adc_dma_endpoint;
    bool spi_dma_endpoint;
    bool uart_dma_endpoint;
    char *flash_file;
    char *ospi2_flash_file;
    Notifier flash_shutdown_notifier;
    uint64_t last_imu_sequence;
    uint64_t last_imu_time_ns;
    bool gpio_ready;
    bool board_profile_locked;
    bool user_key_pressed;
    uint64_t fdcan_kernel_clock_hz;
    uint64_t apb1_timer_clock_hz;
    uint64_t apb2_timer_clock_hz;
    uint64_t usart16_kernel_clock_hz;
    uint64_t usart234578_kernel_clock_hz;
    uint16_t external_gpio[DM_MC02_GPIO_BANKS];
    char *gpio_input_spec;
};

static void dm_mc02_bmi088_accel_consumed(void *opaque)
{
    DmMc02MachineState *s = opaque;

    if (s && s->imu_consume_step_valid) {
        dm_mc02_cosim_link_notify_imu_consumed(
            &s->cosim, s->imu_consume_step_id, UINT32_C(1) << 0);
    }
}

static void dm_mc02_bmi088_gyro_consumed(void *opaque)
{
    DmMc02MachineState *s = opaque;

    if (s && s->imu_consume_step_valid) {
        dm_mc02_cosim_link_notify_imu_consumed(
            &s->cosim, s->imu_consume_step_id, UINT32_C(1) << 1);
    }
}

static DmMc02Dma *dm_mc02_board_dma(DmMc02MachineState *s,
                                    unsigned controller)
{
    switch (controller) {
    case 0:
        return &s->dma_subsystem.dma[0];
    case 1:
        return &s->dma_subsystem.dma[1];
    default:
        return NULL;
    }
}

static const DmMc02BoardFlashProfile *dm_mc02_board_flash(
    const DmMc02BoardProfile *board, unsigned ospi_index)
{
    for (size_t i = 0; i < board->flash_profile_count; ++i) {
        if (board->flash_profiles[i].ospi_index == ospi_index) {
            return &board->flash_profiles[i];
        }
    }
    return NULL;
}

#define TYPE_DM_MC02_MACHINE MACHINE_TYPE_NAME("dm-mc02")
OBJECT_DECLARE_SIMPLE_TYPE(DmMc02MachineState, DM_MC02_MACHINE)

/* Deterministic endpoint fixture used by protocol smoke tests and by users
 * validating a new host adapter.  It is opt-in and deliberately does not
 * model a motor plant: each accepted command advances a counter and returns
 * a canonical state payload for the same motor indices. */
static bool dm_mc02_cosim_motor_loopback(
    void *opaque, uint64_t step_id, uint64_t t_sim_ns, uint64_t dt_ns,
    const uint8_t *command_payload, size_t command_payload_len,
    uint8_t *state_payload, size_t state_payload_capacity,
    size_t *state_payload_len)
{
    DmMc02MachineState *s = opaque;
    uint16_t motor_count;
    size_t expected_command_len;
    size_t state_len;

    (void)step_id;
    (void)t_sim_ns;
    (void)dt_ns;
    *state_payload_len = 0;
    if (!s || !command_payload || command_payload_len < 4 ||
        !state_payload) {
        return false;
    }
    motor_count = lduw_le_p(command_payload);
    expected_command_len = 4u + (size_t)motor_count * 6u;
    state_len = 2u + (size_t)motor_count * 14u;
    if (expected_command_len != command_payload_len ||
        state_len > state_payload_capacity) {
        return false;
    }

    s->cosim_motor_loopback_steps++;
    stw_le_p(state_payload, motor_count);
    for (unsigned i = 0; i < motor_count; ++i) {
        const uint8_t *command = command_payload + 4u + i * 6u;
        uint8_t *state = state_payload + 2u + i * 14u;
        uint16_t index = lduw_le_p(command);
        uint32_t command_bits = ldl_le_p(command + 2);
        uint32_t step_bits;
        float step_value = (float)s->cosim_motor_loopback_steps;

        memcpy(&step_bits, &step_value, sizeof(step_bits));
        stw_le_p(state, index);
        stl_le_p(state + 2, step_bits);
        stl_le_p(state + 6, command_bits);
        stl_le_p(state + 10, command_bits);
    }
    *state_payload_len = state_len;
    return true;
}

static bool dm_mc02_board_pin_valid(const DmMc02MachineState *s,
                                    DmMc02BoardPin pin)
{
    return s && s->board && s->board->soc &&
           pin.bank < s->board->soc->gpio_bank_count && pin.pin < 16;
}

static uint32_t dm_mc02_board_pin_mask(const DmMc02MachineState *s,
                                       DmMc02BoardPin pin)
{
    return dm_mc02_board_pin_valid(s, pin) ? UINT32_C(1) << pin.pin : 0;
}

static bool dm_mc02_board_pin_high(const DmMc02MachineState *s,
                                   DmMc02BoardPin pin)
{
    return dm_mc02_board_pin_valid(s, pin) &&
           (s->gpio_exti.gpio[pin.bank].odr & dm_mc02_board_pin_mask(s, pin));
}

static bool dm_mc02_get_buzzer_enabled(Object *obj, Error **errp);
static bool dm_mc02_get_buzzer_level(Object *obj, Error **errp);
static char *dm_mc02_get_buzzer_frequency(Object *obj, Error **errp);
static char *dm_mc02_get_buzzer_duty(Object *obj, Error **errp);
static char *dm_mc02_get_tim1_ch1_output(Object *obj, Error **errp);
static char *dm_mc02_get_tim8_ch1_output(Object *obj, Error **errp);

static bool dm_mc02_get_fdcan_host_ack(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->fdcan_host_ack;
}

static void dm_mc02_set_fdcan_host_ack(Object *obj, bool enabled,
                                       Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->fdcan_host_ack = enabled;
    for (size_t i = 0; i < s->initialized_fdcan_count; ++i) {
        dm_mc02_fdcan_set_host_ack(&s->fdcan[i], enabled);
    }
}

static char *dm_mc02_get_iwdg_diagnostics(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("starts=%" PRIu64 ",reloads=%" PRIu64
                           ",timeouts=%" PRIu64 ",window-violations=%" PRIu64
                           ",lsi-hz=%u,lsi-error-ppm=%" PRId32
                           ",effective-lsi-hz=%" PRIu64,
                           s->iwdg1.start_count, s->iwdg1.reload_count,
                           s->iwdg1.timeout_count,
                           s->iwdg1.window_violation_count,
                           s->iwdg1.lsi_hz, s->iwdg1.lsi_error_ppm,
                           dm_mc02_iwdg_effective_lsi_hz(
                               s->iwdg1.lsi_hz, s->iwdg1.lsi_error_ppm));
}

static char *dm_mc02_get_wwdg_diagnostics(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("starts=%" PRIu64 ",reloads=%" PRIu64
                           ",ewi=%" PRIu64 ",timeouts=%" PRIu64
                           ",window-violations=%" PRIu64 ",clock-hz=%" PRIu64,
                           s->wwdg1.start_count, s->wwdg1.reload_count,
                           s->wwdg1.ewi_count, s->wwdg1.timeout_count,
                           s->wwdg1.window_violation_count,
                           s->wwdg1.clock_hz);
}

static char *dm_mc02_get_cosim_diagnostics(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    DmMc02CosimLink *link = &s->cosim;

    (void)errp;
    return g_strdup_printf(
        "enabled=%u,opened=%u,rx_protocol=%u,v2_initialized=%u"
        ",v2_session=%u,v2_next_session=%u,tx_sequence=%" PRIu64
        ",rx_frames=%" PRIu64
        ",rx_bad_frames=%" PRIu64 ",rx_dropped_bytes=%" PRIu64
        ",tx_frames=%" PRIu64 ",tx_dropped=%" PRIu64
        ",tx_short_writes=%" PRIu64 ",imu_dropped=%" PRIu64
        ",consume_dropped=%" PRIu64 ",adc_dropped=%" PRIu64
        ",imu_queue=%u,adc_queue=%u,consume_queue=%u,tx_queue=%u"
        ",telemetry_pending=%u",
        link->enabled, link->opened, link->rx_protocol_version,
        link->v2_initialized, link->v2_session_id,
        link->v2_next_session_id, link->tx_sequence, link->rx_frames,
        link->rx_bad_frames, link->rx_dropped_bytes, link->tx_frames,
        link->tx_dropped, link->tx_short_writes, link->imu_dropped,
        link->consume_dropped, link->adc_dropped, link->imu_queue_count,
        link->adc_queue_count, link->consume_count, link->tx_queue_count,
        link->telemetry_pending);
}

static char *dm_mc02_get_iwdg_boot_grace(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("%u", s->iwdg_boot_grace_ms);
}

static char *dm_mc02_get_iwdg_lsi_hz(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("%u", s->iwdg_lsi_hz);
}

static char *dm_mc02_get_iwdg_lsi_error_ppm(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("%" PRId32, s->iwdg_lsi_error_ppm);
}

static char *dm_mc02_get_board_profile(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup(s->board ? s->board->name : "");
}

static void dm_mc02_set_board_profile(Object *obj, const char *value,
                                      Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    const DmMc02BoardProfile *board;
    DmMc02BoardPin user_key;

    if (s->board_profile_locked) {
        error_setg(errp, "board-profile cannot be changed after machine initialization");
        return;
    }
    board = dm_mc02_board_lookup(value);
    if (!board) {
        error_setg(errp, "unknown DM-MC02 board profile '%s'", value);
        return;
    }
    s->board = board;
    g_free(s->gpio_input_spec);
    s->gpio_input_spec = NULL;
    memset(s->external_gpio, 0, sizeof(s->external_gpio));
    user_key = board->devices.user_key;
    if (dm_mc02_board_pin_valid(s, user_key)) {
        s->external_gpio[user_key.bank] = UINT16_C(1) << user_key.pin;
        s->gpio_input_spec = g_strdup_printf("%c%u=1", 'A' + user_key.bank,
                                             user_key.pin);
    }
}

static void dm_mc02_apply_gpio_inputs(DmMc02MachineState *s);
static void dm_mc02_reset_spi_cs(DmMc02MachineState *s);
static void dm_mc02_apply_power_state(DmMc02MachineState *s);
static void dm_mc02_apply_mcu_power_state(DmMc02MachineState *s);

static void dm_mc02_power_brownout(void *opaque)
{
    DmMc02MachineState *s = opaque;

    if (!s) {
        return;
    }
    /* The power model is the producer; PWR/RCC owns the reset-cause latch,
     * and QEMU's normal reset machinery owns reset fan-out. */
    dm_mc02_pwr_rcc_note_brownout_reset(&s->pwr_rcc);
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void dm_mc02_pin_reset(void *opaque)
{
    DmMc02MachineState *s = opaque;

    if (!s) {
        return;
    }
    /* The external pin is the producer; latch the cause before QEMU consumes
     * the reset request, just like the watchdog and brownout boundaries. */
    dm_mc02_pwr_rcc_note_pin_reset(&s->pwr_rcc);
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void dm_mc02_reset_spi_cs_hook(void *opaque)
{
    dm_mc02_reset_spi_cs(opaque);
}

static void dm_mc02_apply_gpio_inputs_hook(void *opaque)
{
    dm_mc02_apply_gpio_inputs(opaque);
}

static void dm_mc02_apply_power_hook(void *opaque)
{
    DmMc02MachineState *s = opaque;

    dm_mc02_apply_power_state(s);
    dm_mc02_apply_mcu_power_state(s);
}

static void dm_mc02_syscfg_changed(void *opaque)
{
    dm_mc02_apply_gpio_inputs(opaque);
}

static void dm_mc02_reset_spi_cs(DmMc02MachineState *s)
{
    uint32_t masks[DM_MC02_GPIO_BANKS] = { 0 };
    uint32_t high[DM_MC02_GPIO_BANKS] = { 0 };

    /* Build bank masks first so routes sharing a GPIO bank produce one
     * coherent ODR update. */
    for (size_t i = 0; i < s->board->spi_cs_route_count; ++i) {
        const DmMc02BoardSpiCsRoute *route = &s->board->spi_cs_routes[i];

        if (dm_mc02_board_pin_valid(s, route->pin)) {
            uint32_t mask = dm_mc02_board_pin_mask(s, route->pin);

            masks[route->pin.bank] |= mask;
            if (route->active_low) {
                high[route->pin.bank] |= mask;
            }
        }
    }
    for (unsigned bank = 0; bank < ARRAY_SIZE(masks); ++bank) {
        if (masks[bank]) {
            dm_mc02_gpio_set_odr(
                &s->gpio_exti.gpio[bank],
                (s->gpio_exti.gpio[bank].odr & ~masks[bank]) |
                                    high[bank]);
        }
    }
}

static bool dm_mc02_get_user_key(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->user_key_pressed;
}

static void dm_mc02_set_user_key(Object *obj, bool pressed, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    DmMc02BoardPin pin = s->board->devices.user_key;
    uint32_t mask = dm_mc02_board_pin_mask(s, pin);

    (void)errp;
    s->user_key_pressed = pressed;
    if (dm_mc02_board_pin_valid(s, pin)) {
        s->external_gpio[pin.bank] =
            (s->external_gpio[pin.bank] & ~mask) |
            (pressed ? 0 : mask);
    }
    g_free(s->gpio_input_spec);
    s->gpio_input_spec = g_strdup_printf("%c%u=%u", 'A' + pin.bank,
                                         pin.pin, pressed ? 0 : 1);
    if (s->gpio_ready) {
        dm_mc02_apply_gpio_inputs(s);
    }
}

static char *dm_mc02_get_gpio_input(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup(s->gpio_input_spec ? s->gpio_input_spec : "A15=1");
}

static bool dm_mc02_parse_gpio_input(const char *value, unsigned *bank,
                                     unsigned *pin, bool *level,
                                     Error **errp)
{
    char port;
    char *end;
    guint64 parsed_pin;

    if (!value || !value[0] || value[1] == '\0') {
        error_setg(errp, "GPIO input must use PORTPIN=0|1, for example A15=0");
        return false;
    }
    port = g_ascii_toupper(value[0]);
    if (port < 'A' || port >= 'A' + DM_MC02_GPIO_BANKS) {
        error_setg(errp, "GPIO input port must be A through H");
        return false;
    }
    parsed_pin = g_ascii_strtoull(value + 1, &end, 10);
    if (end == value + 1 || (*end != '=' && *end != ':') ||
        parsed_pin >= 16 || (end[1] != '0' && end[1] != '1') ||
        end[2] != '\0') {
        error_setg(errp, "GPIO input must use PORTPIN=0|1, for example A15=0");
        return false;
    }
    *bank = port - 'A';
    *pin = parsed_pin;
    *level = end[1] == '1';
    return true;
}

static void dm_mc02_set_gpio_input(Object *obj, const char *value,
                                    Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    unsigned bank;
    unsigned pin;
    bool level;

    if (!dm_mc02_parse_gpio_input(value, &bank, &pin, &level, errp)) {
        return;
    }
    s->external_gpio[bank] = (s->external_gpio[bank] &
                              ~(UINT16_C(1) << pin)) |
                             (level ? UINT16_C(1) << pin : 0);
    if (s->board && bank == s->board->devices.user_key.bank &&
        pin == s->board->devices.user_key.pin) {
        s->user_key_pressed = !level;
    }
    g_free(s->gpio_input_spec);
    s->gpio_input_spec = g_strdup(value);
    if (s->gpio_ready) {
        dm_mc02_apply_gpio_inputs(s);
    }
}

static void dm_mc02_apply_gpio_inputs(DmMc02MachineState *s)
{
    if (!s || !s->gpio_ready) {
        return;
    }
    unsigned bank_count = s->board && s->board->soc ?
                           s->board->soc->gpio_bank_count :
                           DM_MC02_GPIO_BANKS;

    for (unsigned bank = 0; bank < bank_count; ++bank) {
        dm_mc02_gpio_set_input(&s->gpio_exti.gpio[bank], UINT16_MAX,
                               s->external_gpio[bank]);
    }
    for (unsigned line = 0; line < 16; ++line) {
        unsigned bank = dm_mc02_syscfg_get_exti_port(&s->gpio_exti.syscfg,
                                                     line);

        if (bank < bank_count) {
            dm_mc02_exti_set_line(&s->gpio_exti.exti, line,
                                  (s->external_gpio[bank] >> line) & 1u);
        }
    }
}

static void dm_mc02_set_iwdg_boot_grace(Object *obj, const char *value,
                                        Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    char *end = NULL;
    guint64 parsed;

    if (!value || value[0] == '-') {
        error_setg(errp, "IWDG boot grace must be an unsigned millisecond value");
        return;
    }
    parsed = g_ascii_strtoull(value, &end, 0);
    if (end == value || *end != '\0' || parsed > UINT32_MAX) {
        error_setg(errp, "IWDG boot grace must be an unsigned millisecond value");
        return;
    }
    s->iwdg_boot_grace_ms = (uint32_t)parsed;
    dm_mc02_iwdg_set_boot_grace_ms(&s->iwdg1, s->iwdg_boot_grace_ms);
}

static void dm_mc02_set_iwdg_lsi_hz(Object *obj, const char *value,
                                    Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    char *end = NULL;
    guint64 parsed;

    if (s->iwdg1.started) {
        error_setg(errp, "IWDG LSI configuration is startup-only while running");
        return;
    }
    if (!value || value[0] == '-') {
        error_setg(errp, "IWDG LSI frequency must be a positive Hz value");
        return;
    }
    parsed = g_ascii_strtoull(value, &end, 0);
    if (end == value || *end != '\0' || parsed == 0 ||
        parsed > UINT32_MAX) {
        error_setg(errp, "IWDG LSI frequency must be a positive Hz value");
        return;
    }
    s->iwdg_lsi_hz = (uint32_t)parsed;
    if (s->iwdg1.timeout_timer) {
        dm_mc02_iwdg_set_lsi_hz(&s->iwdg1, s->iwdg_lsi_hz);
    }
}

static void dm_mc02_set_iwdg_lsi_error_ppm(Object *obj, const char *value,
                                           Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    char *end = NULL;
    gint64 parsed;

    if (s->iwdg1.started) {
        error_setg(errp, "IWDG LSI configuration is startup-only while running");
        return;
    }
    if (!value || !value[0]) {
        error_setg(errp, "IWDG LSI error must be a signed ppm value in [%d,%d]",
                   DM_MC02_IWDG_MIN_LSI_ERROR_PPM,
                   DM_MC02_IWDG_MAX_LSI_ERROR_PPM);
        return;
    }
    parsed = g_ascii_strtoll(value, &end, 0);
    if (end == value || *end != '\0' ||
        parsed < DM_MC02_IWDG_MIN_LSI_ERROR_PPM ||
        parsed > DM_MC02_IWDG_MAX_LSI_ERROR_PPM) {
        error_setg(errp, "IWDG LSI error must be a signed ppm value in [%d,%d]",
                   DM_MC02_IWDG_MIN_LSI_ERROR_PPM,
                   DM_MC02_IWDG_MAX_LSI_ERROR_PPM);
        return;
    }
    s->iwdg_lsi_error_ppm = (int32_t)parsed;
    if (s->iwdg1.timeout_timer) {
        dm_mc02_iwdg_set_lsi_error_ppm(&s->iwdg1,
                                       s->iwdg_lsi_error_ppm);
    }
}

static void dm_mc02_apply_power_state(DmMc02MachineState *s)
{
    bool system_5v_good;

    if (!s->power.adc) {
        return;
    }
    system_5v_good = dm_mc02_power_get_system_5v_good(&s->power);
    for (size_t i = 0; i < s->initialized_fdcan_count; ++i) {
        dm_mc02_fdcan_set_powered(&s->fdcan[i], system_5v_good);
    }
    /* Only external RS485 transceivers are behind the switched 5 V rail.
     * Select them from the board profile instead of relying on UART array
     * positions; the MCU USARTs themselves remain usable. */
    for (size_t i = 0; i < s->initialized_uart_count; ++i) {
        if (s->board->uarts[i].rs485) {
            dm_mc02_uart_set_powered(&s->uart[i], system_5v_good);
        }
    }
}

static void dm_mc02_apply_mcu_power_state(DmMc02MachineState *s)
{
    CPUState *cpu;
    bool powered;

    if (!s || !s->power.adc) {
        return;
    }
    cpu = first_cpu;
    if (!cpu) {
        return;
    }

    /* VIN=0 is a board-level power-removal boundary, even in ideal_power
     * mode.  A transition resets the core; a repeated machine reset only
     * needs to restore the halted state because QEMU has already reset it. */
    powered = dm_mc02_power_get_state(&s->power) != DM_MC02_POWER_OFF;
    if (powered != s->mcu_powered) {
        cpu_reset(cpu);
    }
    cpu->halted = powered ? 0 : 1;
    cpu->exception_index = powered ? -1 : EXCP_HLT;
    qemu_cpu_kick(cpu);
    s->mcu_powered = powered;
}

static bool dm_mc02_get_mcu_power_good(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->mcu_powered;
}

static bool dm_mc02_get_out1_enabled(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return dm_mc02_power_get_out1_enabled(&s->power);
}

static bool dm_mc02_get_out2_enabled(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return dm_mc02_power_get_out2_enabled(&s->power);
}

static bool dm_mc02_get_5v_switch_enabled(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return dm_mc02_power_get_5v_enabled(&s->power);
}

static bool dm_mc02_get_system_5v_good(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return dm_mc02_power_get_system_5v_good(&s->power);
}

static bool dm_mc02_get_system_3v3_good(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return dm_mc02_power_get_system_3v3_good(&s->power);
}

static bool dm_mc02_get_out1_good(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return dm_mc02_power_get_out1_good(&s->power);
}

static bool dm_mc02_get_out2_good(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return dm_mc02_power_get_out2_good(&s->power);
}

static bool dm_mc02_get_electrical_power(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->electrical_power;
}

static void dm_mc02_set_electrical_power(Object *obj, bool enabled,
                                          Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->electrical_power = enabled;
    if (s->power.adc) {
        dm_mc02_power_set_electrical_policy(&s->power, enabled);
        dm_mc02_apply_power_state(s);
        dm_mc02_apply_mcu_power_state(s);
    }
}

static char *dm_mc02_get_vin_mv(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("%u", s->vin_mv);
}

static void dm_mc02_set_vin_mv(Object *obj, const char *value,
                               Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    char *end = NULL;
    guint64 parsed;

    if (!value || value[0] == '-') {
        error_setg(errp, "VIN must be an unsigned millivolt value");
        return;
    }
    parsed = g_ascii_strtoull(value, &end, 0);
    if (end == value || *end != '\0' || parsed > UINT32_MAX) {
        error_setg(errp, "VIN must be an unsigned value in millivolts");
        return;
    }
    s->vin_mv = (uint32_t)parsed;
    if (s->power.adc) {
        dm_mc02_power_set_vin_mv(&s->power, s->vin_mv);
        dm_mc02_apply_power_state(s);
        dm_mc02_apply_mcu_power_state(s);
    }
}

static bool dm_mc02_get_cold_reset(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->cold_reset;
}

static void dm_mc02_set_cold_reset(Object *obj, bool enabled,
                                   Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->cold_reset = enabled;
}

static bool dm_mc02_get_accurate_timing(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->accurate_timing;
}

static bool dm_mc02_get_adc_accurate_timing(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->adc_accurate_timing;
}

static bool dm_mc02_get_adc_power_model(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->adc_power_model;
}

static void dm_mc02_set_adc_power_model(Object *obj, bool enabled,
                                        Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->adc_power_model = enabled;
    if (s->adc[0].sample_timer) {
        dm_mc02_adc_set_power_model(&s->adc[0], enabled);
        dm_mc02_adc_set_power_model(&s->adc[1], enabled);
    }
}

static bool dm_mc02_get_adc_dma_endpoint(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->adc_dma_endpoint;
}

static void dm_mc02_set_adc_dma_endpoint(Object *obj, bool enabled,
                                          Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->adc_dma_endpoint = enabled;
    dm_mc02_adc_set_dma_endpoint(&s->adc[0], enabled);
    dm_mc02_adc_set_dma_endpoint(&s->adc[1], enabled);
}

static bool dm_mc02_get_spi_dma_endpoint(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->spi_dma_endpoint;
}

static void dm_mc02_set_spi_dma_endpoint(Object *obj, bool enabled,
                                          Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->spi_dma_endpoint = enabled;
    dm_mc02_spi_set_dma_endpoint(&s->bmi_spi_link.spi, enabled);
}

static bool dm_mc02_get_uart_dma_endpoint(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->uart_dma_endpoint;
}

static void dm_mc02_set_uart_dma_endpoint(Object *obj, bool enabled,
                                           Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->uart_dma_endpoint = enabled;
    for (size_t i = 0; i < s->initialized_uart_count; ++i) {
        dm_mc02_uart_set_dma_endpoint(&s->uart[i], enabled);
    }
}

static void dm_mc02_set_adc_accurate_timing(Object *obj, bool enabled,
                                             Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->adc_accurate_timing = enabled;
    dm_mc02_adc_set_accurate_timing(&s->adc[0], enabled);
    dm_mc02_adc_set_accurate_timing(&s->adc[1], enabled);
}

static void dm_mc02_set_accurate_timing(Object *obj, bool enabled,
                                         Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->accurate_timing = enabled;
    if (s->board && s->board->devices.tim8_index < s->initialized_timer_count &&
        s->tim_aux[s->board->devices.tim8_index].update_timer) {
        dm_mc02_tim2_set_update_batch(
                                      &s->tim_aux[s->board->devices.tim8_index],
                                      enabled ? 1 :
                                      DM_MC02_WS2812_APPROX_BATCH);
        dm_mc02_tim2_set_compare_batch(
            &s->tim_aux[s->board->devices.tim8_index], enabled ? 1 :
            DM_MC02_WS2812_APPROX_BATCH);
    }
}

static bool dm_mc02_set_noise(Object *obj, const char *value, bool accel,
                               Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    char *end = NULL;
    double parsed = g_ascii_strtod(value, &end);

    if (end == value || *end != '\0' || !isfinite(parsed) || parsed < 0.0) {
        error_setg(errp, "IMU noise must be a finite non-negative number");
        return false;
    }
    if (accel) {
        dm_mc02_bmi088_set_noise(&s->bmi_spi_link.accel, parsed);
    } else {
        dm_mc02_bmi088_set_noise(&s->bmi_spi_link.gyro, parsed);
    }
    return true;
}

static char *dm_mc02_get_gyro_noise(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("%.9g",
                           dm_mc02_bmi088_get_noise(&s->bmi_spi_link.gyro));
}

static char *dm_mc02_get_accel_noise(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("%.9g",
                           dm_mc02_bmi088_get_noise(&s->bmi_spi_link.accel));
}

static char *dm_mc02_get_imu_temperature(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup_printf("%.9g",
                           dm_mc02_bmi088_get_temperature(
                               &s->bmi_spi_link.accel));
}

static void dm_mc02_set_imu_temperature(Object *obj, const char *value,
                                         Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    char *end = NULL;
    double parsed = g_ascii_strtod(value, &end);

    if (end == value || *end != '\0' || !isfinite(parsed)) {
        error_setg(errp, "IMU temperature must be a finite number");
        return;
    }
    /* Both BMI088 dies share the same board temperature input.  Keep the
     * signal-layer temperature in sync so gyro temperature coefficients are
     * observable through the same machine property as accel coefficients. */
    dm_mc02_bmi088_set_temperature(&s->bmi_spi_link.accel, parsed);
    dm_mc02_bmi088_set_temperature(&s->bmi_spi_link.gyro, parsed);
}

static void dm_mc02_set_gyro_noise(Object *obj, const char *value,
                                    Error **errp)
{
    dm_mc02_set_noise(obj, value, false, errp);
}

static void dm_mc02_set_accel_noise(Object *obj, const char *value,
                                     Error **errp)
{
    dm_mc02_set_noise(obj, value, true, errp);
}

static bool dm_mc02_parse_bias(const char *value, double bias[3],
                               Error **errp)
{
    /* ':' is accepted as the CLI-friendly separator because commas are
     * consumed by QEMU's -machine property list. */
    g_auto(GStrv) parts = g_strsplit_set(value, ",:", 0);
    char *end;

    if (!parts || !parts[0] || !parts[1] || !parts[2] || parts[3]) {
        error_setg(errp, "IMU bias must contain three comma- or colon-separated values");
        return false;
    }
    for (unsigned i = 0; i < 3; ++i) {
        end = NULL;
        bias[i] = g_ascii_strtod(parts[i], &end);
        if (end == parts[i] || *end != '\0' || !isfinite(bias[i])) {
            error_setg(errp, "IMU bias values must be finite numbers");
            return false;
        }
    }
    return true;
}

static bool dm_mc02_parse_vector(const char *value, double vector[3],
                                 const char *description, Error **errp)
{
    g_auto(GStrv) parts = g_strsplit_set(value, ",:", 0);
    char *end;

    if (!parts || !parts[0] || !parts[1] || !parts[2] || parts[3]) {
        error_setg(errp, "%s must contain three comma- or colon-separated values",
                   description);
        return false;
    }
    for (unsigned i = 0; i < 3; ++i) {
        end = NULL;
        vector[i] = g_ascii_strtod(parts[i], &end);
        if (end == parts[i] || *end != '\0' || !isfinite(vector[i])) {
            error_setg(errp, "%s values must be finite numbers", description);
            return false;
        }
    }
    return true;
}

static char *dm_mc02_get_imu_vector(Object *obj, bool accel, bool random_walk,
                                    Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    double vector[DM_MC02_BMI088_SIGNAL_AXES];

    (void)errp;
    if (random_walk) {
        dm_mc02_bmi088_get_bias_random_walk(
            accel ? &s->bmi_spi_link.accel : &s->bmi_spi_link.gyro, vector);
    } else {
        dm_mc02_bmi088_get_temperature_coefficient(
            accel ? &s->bmi_spi_link.accel : &s->bmi_spi_link.gyro, vector);
    }
    return g_strdup_printf("%.9g,%.9g,%.9g", vector[0], vector[1], vector[2]);
}

static void dm_mc02_set_imu_vector(Object *obj, const char *value, bool accel,
                                   bool random_walk, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    double vector[DM_MC02_BMI088_SIGNAL_AXES];
    const char *description = random_walk ?
        "IMU bias random walk standard deviation" :
        "IMU temperature coefficient";

    if (!dm_mc02_parse_vector(value, vector, description, errp)) {
        return;
    }
    if (random_walk) {
        for (unsigned i = 0; i < ARRAY_SIZE(vector); ++i) {
            if (vector[i] < 0.0) {
                error_setg(errp, "%s values must be non-negative", description);
                return;
            }
        }
        dm_mc02_bmi088_set_bias_random_walk(
            accel ? &s->bmi_spi_link.accel : &s->bmi_spi_link.gyro, vector);
    } else {
        dm_mc02_bmi088_set_temperature_coefficient(
            accel ? &s->bmi_spi_link.accel : &s->bmi_spi_link.gyro, vector);
    }
}

static char *dm_mc02_get_gyro_temperature_coefficient(Object *obj,
                                                       Error **errp)
{
    return dm_mc02_get_imu_vector(obj, false, false, errp);
}

static char *dm_mc02_get_accel_temperature_coefficient(Object *obj,
                                                        Error **errp)
{
    return dm_mc02_get_imu_vector(obj, true, false, errp);
}

static char *dm_mc02_get_gyro_bias_random_walk(Object *obj, Error **errp)
{
    return dm_mc02_get_imu_vector(obj, false, true, errp);
}

static char *dm_mc02_get_accel_bias_random_walk(Object *obj, Error **errp)
{
    return dm_mc02_get_imu_vector(obj, true, true, errp);
}

static void dm_mc02_set_gyro_temperature_coefficient(Object *obj,
                                                      const char *value,
                                                      Error **errp)
{
    dm_mc02_set_imu_vector(obj, value, false, false, errp);
}

static void dm_mc02_set_accel_temperature_coefficient(Object *obj,
                                                       const char *value,
                                                       Error **errp)
{
    dm_mc02_set_imu_vector(obj, value, true, false, errp);
}

static void dm_mc02_set_gyro_bias_random_walk(Object *obj, const char *value,
                                              Error **errp)
{
    dm_mc02_set_imu_vector(obj, value, false, true, errp);
}

static void dm_mc02_set_accel_bias_random_walk(Object *obj, const char *value,
                                               Error **errp)
{
    dm_mc02_set_imu_vector(obj, value, true, true, errp);
}

static char *dm_mc02_get_gyro_bias(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    double bias[DM_MC02_BMI088_SIGNAL_AXES];

    dm_mc02_bmi088_get_bias(&s->bmi_spi_link.gyro, bias);
    return g_strdup_printf("%.9g,%.9g,%.9g", bias[0], bias[1], bias[2]);
}

static char *dm_mc02_get_accel_bias(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    double bias[DM_MC02_BMI088_SIGNAL_AXES];

    dm_mc02_bmi088_get_bias(&s->bmi_spi_link.accel, bias);
    return g_strdup_printf("%.9g,%.9g,%.9g", bias[0], bias[1], bias[2]);
}

static void dm_mc02_set_gyro_bias(Object *obj, const char *value,
                                   Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    double bias[3];

    if (dm_mc02_parse_bias(value, bias, errp)) {
        dm_mc02_bmi088_set_bias(&s->bmi_spi_link.gyro, bias);
    }
}

static void dm_mc02_set_accel_bias(Object *obj, const char *value,
                                    Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    double bias[3];

    if (dm_mc02_parse_bias(value, bias, errp)) {
        dm_mc02_bmi088_set_bias(&s->bmi_spi_link.accel, bias);
    }
}

static char *dm_mc02_get_flash_file(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup(s->flash_file ? s->flash_file : "");
}

static void dm_mc02_set_flash_file(Object *obj, const char *value,
                                   Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    if (s->board_profile_locked) {
        error_setg(errp,
                   "flash-file cannot be changed after machine initialization");
        return;
    }
    g_free(s->flash_file);
    s->flash_file = g_strdup(value);
}

static char *dm_mc02_get_ospi2_flash_file(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return g_strdup(s->ospi2_flash_file ? s->ospi2_flash_file : "");
}

static void dm_mc02_set_ospi2_flash_file(Object *obj, const char *value,
                                         Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    if (s->board_profile_locked) {
        error_setg(errp,
                   "ospi2-flash-file cannot be changed after machine initialization");
        return;
    }
    g_free(s->ospi2_flash_file);
    s->ospi2_flash_file = g_strdup(value);
}

static bool dm_mc02_get_cosim_motor_loopback(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    return s->cosim_motor_loopback;
}

static void dm_mc02_set_cosim_motor_loopback(Object *obj, bool enabled,
                                              Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    (void)errp;
    s->cosim_motor_loopback = enabled;
    if (s->cosim.opened) {
        dm_mc02_cosim_link_set_motor_step_handler(
            &s->cosim, enabled ? dm_mc02_cosim_motor_loopback : NULL);
    }
}

static void dm_mc02_persist_flash(Notifier *notifier, void *opaque)
{
    DmMc02MachineState *s = container_of(notifier, DmMc02MachineState,
                                         flash_shutdown_notifier);
    DmNorFlashPersistenceResult result;

    (void)opaque;
    if (s->flash_file && s->flash_file[0]) {
        result = dm_nor_flash_persistence_save(
            s->flash_file,
            memory_region_get_ram_ptr(&s->soc_memory.flash),
            s->board->soc->flash_size);
        if (result != DM_NOR_FLASH_PERSISTENCE_OK) {
            warn_report("unable to persist DM-MC02 internal Flash to '%s' "
                        "(result=%d)", s->flash_file, result);
        }
    }
    if (s->ospi2_flash_file && s->ospi2_flash_file[0]) {
        result = dm_mc02_ospi_save_persistence(&s->ospi2,
                                               s->ospi2_flash_file);
        if (result != DM_NOR_FLASH_PERSISTENCE_OK) {
            warn_report("unable to persist DM-MC02 OCTOSPI2 Flash to '%s' "
                        "(result=%d)", s->ospi2_flash_file, result);
        }
    }
}

static void dm_mc02_machine_instance_init(Object *obj)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    DmMc02BoardPin user_key;

    s->board = dm_mc02_board_dm_mc02();
    object_initialize_child(obj, "reset-input", &s->reset_input,
                            TYPE_DM_MC02_RESET_INPUT);
    object_property_add_link(obj, "canbus", TYPE_CAN_BUS,
                             (Object **)&s->canbus,
                             object_property_allow_set_link,
                             OBJ_PROP_LINK_STRONG);
    object_property_add_str(obj, "board-profile",
                            dm_mc02_get_board_profile,
                            dm_mc02_set_board_profile);
    s->imu_seed = 1;
    s->vin_mv = DM_MC02_POWER_DEFAULT_VIN_MV;
    /* Treat the machine instance as powered before the board input is
     * applied.  This makes an initial VIN=0 configuration an explicit
     * power-loss transition, so the core receives a reset before QEMU can
     * release it from the normal startup path. */
    s->mcu_powered = true;
    user_key = s->board->devices.user_key;
    s->external_gpio[user_key.bank] = UINT16_C(1) << user_key.pin;
    s->gpio_input_spec = g_strdup_printf("%c%u=1", 'A' + user_key.bank,
                                         user_key.pin);
    /* The current host has enough headroom for the real board ADC cadence;
     * keep the machine behavior faithful by default.  Users on slower hosts
     * can opt into the bounded legacy cadence with adc-accurate-timing=off. */
    s->adc_accurate_timing = true;
    s->adc_dma_endpoint = true;
    s->spi_dma_endpoint = true;
    s->uart_dma_endpoint = true;
    /* Real hardware reaches the first application-level watchdog refresh
     * well within this interval.  QEMU's translated startup can spend more
     * than the configured watchdog period before the scheduler is useful;
     * keep the default usable while exposing 0 for strict hardware timing. */
    s->iwdg_boot_grace_ms = 5000;
    s->iwdg_lsi_hz = DM_MC02_IWDG_DEFAULT_LSI_HZ;
    s->iwdg_lsi_error_ppm = 0;
    dm_mc02_bmi088_spi_link_state_init(&s->bmi_spi_link);
    dm_mc02_spi_state_init(&s->spi1_state);
    dm_mc02_bmi088_spi_link_set_consume_callbacks(
        &s->bmi_spi_link, dm_mc02_bmi088_accel_consumed, s,
        dm_mc02_bmi088_gyro_consumed, s);
    object_property_add_bool(obj, "electrical-power",
                             dm_mc02_get_electrical_power,
                             dm_mc02_set_electrical_power);
    object_property_add_bool(obj, "mcu-power-good",
                             dm_mc02_get_mcu_power_good, NULL);
    object_property_add_bool(obj, "out1-enabled",
                             dm_mc02_get_out1_enabled, NULL);
    object_property_add_bool(obj, "out2-enabled",
                             dm_mc02_get_out2_enabled, NULL);
    object_property_add_bool(obj, "5v-switch-enabled",
                             dm_mc02_get_5v_switch_enabled, NULL);
    object_property_add_bool(obj, "system-5v-good",
                             dm_mc02_get_system_5v_good, NULL);
    object_property_add_bool(obj, "system-3v3-good",
                             dm_mc02_get_system_3v3_good, NULL);
    object_property_add_bool(obj, "out1-good", dm_mc02_get_out1_good, NULL);
    object_property_add_bool(obj, "out2-good", dm_mc02_get_out2_good, NULL);
    object_property_add_bool(obj, "cold-reset",
                             dm_mc02_get_cold_reset,
                             dm_mc02_set_cold_reset);
    object_property_add_bool(obj, "accurate-timing",
                             dm_mc02_get_accurate_timing,
                             dm_mc02_set_accurate_timing);
    object_property_add_bool(obj, "fdcan-host-ack",
                             dm_mc02_get_fdcan_host_ack,
                             dm_mc02_set_fdcan_host_ack);
    object_property_add_uint64_ptr(obj, "fdcan-kernel-clock-hz",
                                   &s->fdcan_kernel_clock_hz,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "apb1-timer-clock-hz",
                                   &s->apb1_timer_clock_hz,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "apb2-timer-clock-hz",
                                   &s->apb2_timer_clock_hz,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "usart16-kernel-clock-hz",
                                   &s->usart16_kernel_clock_hz,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "usart234578-kernel-clock-hz",
                                   &s->usart234578_kernel_clock_hz,
                                   OBJ_PROP_FLAG_READ);
    object_property_add_bool(obj, "adc-accurate-timing",
                             dm_mc02_get_adc_accurate_timing,
                             dm_mc02_set_adc_accurate_timing);
    object_property_add_bool(obj, "adc-power-model",
                             dm_mc02_get_adc_power_model,
                             dm_mc02_set_adc_power_model);
    object_property_add_bool(obj, "adc-dma-endpoint",
                             dm_mc02_get_adc_dma_endpoint,
                             dm_mc02_set_adc_dma_endpoint);
    object_property_add_bool(obj, "spi-dma-endpoint",
                             dm_mc02_get_spi_dma_endpoint,
                             dm_mc02_set_spi_dma_endpoint);
    object_property_add_bool(obj, "uart-dma-endpoint",
                             dm_mc02_get_uart_dma_endpoint,
                             dm_mc02_set_uart_dma_endpoint);
    object_property_add_bool(obj, "user-key", dm_mc02_get_user_key,
                             dm_mc02_set_user_key);
    object_property_add_str(obj, "gpio-input", dm_mc02_get_gpio_input,
                            dm_mc02_set_gpio_input);
    object_property_add_str(obj, "vin-mv", dm_mc02_get_vin_mv,
                            dm_mc02_set_vin_mv);
    object_property_add_str(obj, "imu-noise-gyro-dps",
                            dm_mc02_get_gyro_noise,
                            dm_mc02_set_gyro_noise);
    object_property_add_str(obj, "imu-noise-accel-g",
                            dm_mc02_get_accel_noise,
                            dm_mc02_set_accel_noise);
    object_property_add_str(obj, "imu-temperature-c",
                            dm_mc02_get_imu_temperature,
                            dm_mc02_set_imu_temperature);
    object_property_add_str(obj, "imu-bias-gyro-dps",
                            dm_mc02_get_gyro_bias,
                            dm_mc02_set_gyro_bias);
    object_property_add_str(obj, "imu-bias-accel-g",
                            dm_mc02_get_accel_bias,
                            dm_mc02_set_accel_bias);
    object_property_add_str(obj, "imu-temp-coeff-gyro-dps-per-c",
                            dm_mc02_get_gyro_temperature_coefficient,
                            dm_mc02_set_gyro_temperature_coefficient);
    object_property_add_str(obj, "imu-temp-coeff-accel-g-per-c",
                            dm_mc02_get_accel_temperature_coefficient,
                            dm_mc02_set_accel_temperature_coefficient);
    object_property_add_str(obj, "imu-bias-random-walk-gyro-dps-per-sqrt-s",
                            dm_mc02_get_gyro_bias_random_walk,
                            dm_mc02_set_gyro_bias_random_walk);
    object_property_add_str(obj, "imu-bias-random-walk-accel-g-per-sqrt-s",
                            dm_mc02_get_accel_bias_random_walk,
                            dm_mc02_set_accel_bias_random_walk);
    object_property_add_uint32_ptr(obj, "imu-seed", &s->imu_seed,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "dma-batch-limit",
                                   &s->bmi_spi_link.spi.dma_batch_limit,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_str(obj, "flash-file", dm_mc02_get_flash_file,
                            dm_mc02_set_flash_file);
    object_property_add_str(obj, "ospi2-flash-file",
                            dm_mc02_get_ospi2_flash_file,
                            dm_mc02_set_ospi2_flash_file);
    object_property_add_bool(obj, "cosim-motor-loopback",
                             dm_mc02_get_cosim_motor_loopback,
                             dm_mc02_set_cosim_motor_loopback);
    object_property_add_bool(obj, "buzzer-enabled",
                             dm_mc02_get_buzzer_enabled, NULL);
    object_property_add_bool(obj, "buzzer-level",
                             dm_mc02_get_buzzer_level, NULL);
    object_property_add_str(obj, "buzzer-frequency-hz",
                            dm_mc02_get_buzzer_frequency, NULL);
    object_property_add_str(obj, "buzzer-duty-permille",
                            dm_mc02_get_buzzer_duty, NULL);
    object_property_add_str(obj, "tim1-ch1-output",
                            dm_mc02_get_tim1_ch1_output, NULL);
    object_property_add_str(obj, "tim8-ch1-output",
                            dm_mc02_get_tim8_ch1_output, NULL);
    object_property_add_str(obj, "iwdg-diagnostics",
                            dm_mc02_get_iwdg_diagnostics, NULL);
    object_property_add_str(obj, "wwdg-diagnostics",
                            dm_mc02_get_wwdg_diagnostics, NULL);
    object_property_add_str(obj, "cosim-diagnostics",
                            dm_mc02_get_cosim_diagnostics, NULL);
    object_property_add_str(obj, "iwdg-boot-grace-ms",
                            dm_mc02_get_iwdg_boot_grace,
                            dm_mc02_set_iwdg_boot_grace);
    object_property_add_str(obj, "iwdg-lsi-hz",
                            dm_mc02_get_iwdg_lsi_hz,
                            dm_mc02_set_iwdg_lsi_hz);
    object_property_add_str(obj, "iwdg-lsi-error-ppm",
                            dm_mc02_get_iwdg_lsi_error_ppm,
                            dm_mc02_set_iwdg_lsi_error_ppm);
}

static void dm_mc02_dma_stream_enabled(void *opaque, unsigned stream,
                                        uint32_t control)
{
    DmMc02MachineState *s = opaque;
    const DmMc02BoardDmaUartRoute *route;

    if (!(control & DM_MC02_DMA_CR_EN)) {
        return;
    }
    if ((control & (3u << 6)) == DM_MC02_DMA_CR_DIR_M2P &&
        s->bmi_spi_link.spi.dma_tx.dma == &s->dma_subsystem.dma[0] &&
        stream == s->bmi_spi_link.spi.dma_tx.stream &&
        s->bmi_spi_link.spi.dma_tx_timer) {
        /* Start after the CR write has returned to the guest.  The timer
         * callback advances exactly one item and re-arms itself only while
         * NDTR remains. */
        timer_mod(s->bmi_spi_link.spi.dma_tx_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1);
    }
    if ((control & (3u << 6)) == DM_MC02_DMA_CR_DIR_P2M &&
        s->bmi_spi_link.spi.dma_rx.dma == &s->dma_subsystem.dma[0] &&
        stream == s->bmi_spi_link.spi.dma_rx.stream) {
        dm_mc02_spi_dma_rx_stream_enabled(&s->bmi_spi_link.spi);
    }
    if ((control & (3u << 6)) == DM_MC02_DMA_CR_DIR_P2M) {
        /* ADC1's request source is level-like while ADC_DR/EOC is pending;
         * the DMA endpoint matcher still selects only the configured request
         * and PAR, so unrelated stream enables do not consume it. */
        dm_mc02_adc_dma_stream_enabled(&s->adc[0]);
        dm_mc02_adc_common_dma_stream_enabled(s);
    }
    for (size_t i = 0; i < s->board->dma1_uart_route_count; ++i) {
        route = &s->board->dma1_uart_routes[i];
        if (route->stream != stream) {
            continue;
        }
        if (route->tx && (control & (3u << 6)) == DM_MC02_DMA_CR_DIR_M2P) {
            dm_mc02_uart_dma_tx_stream_enabled(&s->uart[route->uart_index]);
        } else if (!route->tx &&
                   (control & (3u << 6)) == DM_MC02_DMA_CR_DIR_P2M) {
            dm_mc02_uart_dma_rx_stream_enabled(&s->uart[route->uart_index]);
        }
    }
}

static void dm_mc02_uart_dma2_stream_enabled(void *opaque, unsigned stream,
                                             uint32_t control)
{
    DmMc02MachineState *s = opaque;
    const DmMc02BoardDmaUartRoute *route;

    if (!(control & DM_MC02_DMA_CR_EN)) {
        return;
    }
    if ((control & (3u << 6)) == DM_MC02_DMA_CR_DIR_M2P &&
        s->bmi_spi_link.spi.dma_tx.dma == &s->dma_subsystem.dma[1] &&
        stream == s->bmi_spi_link.spi.dma_tx.stream &&
        s->bmi_spi_link.spi.dma_tx_timer) {
        timer_mod(s->bmi_spi_link.spi.dma_tx_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1);
    }
    if ((control & (3u << 6)) == DM_MC02_DMA_CR_DIR_P2M &&
        s->bmi_spi_link.spi.dma_rx.dma == &s->dma_subsystem.dma[1] &&
        stream == s->bmi_spi_link.spi.dma_rx.stream) {
        dm_mc02_spi_dma_rx_stream_enabled(&s->bmi_spi_link.spi);
    }
    for (size_t i = 0; i < s->board->dma2_uart_route_count; ++i) {
        route = &s->board->dma2_uart_routes[i];
        if (route->stream != stream) {
            continue;
        }
        if (route->tx && (control & (3u << 6)) == DM_MC02_DMA_CR_DIR_M2P) {
            dm_mc02_uart_dma_tx_stream_enabled(&s->uart[route->uart_index]);
        } else if (!route->tx &&
                   (control & (3u << 6)) == DM_MC02_DMA_CR_DIR_P2M) {
            dm_mc02_uart_dma_rx_stream_enabled(&s->uart[route->uart_index]);
        }
    }
}

static void dm_mc02_tim8_update(void *opaque)
{
    DmMc02MachineState *s = opaque;
    unsigned timer_index = s->board->devices.tim8_index;
    DmMc02Tim2 *timer = &s->tim_aux[timer_index];
    uint32_t psc = timer->regs[0x28 / sizeof(uint32_t)] & 0xffff;
    uint32_t arr = timer->regs[0x2c / sizeof(uint32_t)];
    unsigned batch = (!s->accurate_timing && psc == 0 && arr == 299) ?
                     DM_MC02_WS2812_APPROX_BATCH : 1;

    if (dm_mc02_tim2_get_update_batch(timer) != batch) {
        dm_mc02_tim2_set_update_batch(timer, batch);
    }
}

static void dm_mc02_tim8_compare(void *opaque, unsigned events,
                                  uint64_t timestamp_ns)
{
    DmMc02MachineState *s = opaque;
    const DmMc02BoardDeviceWiring *devices = &s->board->devices;
    DmMc02Tim2 *timer = &s->tim_aux[devices->tim8_index];
    DmMc02Dma *ws_dma = dm_mc02_board_dma(s,
                                           devices->ws2812_dma_controller);
    uint32_t dier = timer->regs[0x0c / sizeof(uint32_t)];
    uint32_t psc = timer->regs[0x28 / sizeof(uint32_t)] & 0xffff;
    uint32_t arr = timer->regs[0x2c / sizeof(uint32_t)];
    unsigned batch = (!s->accurate_timing && psc == 0 && arr == 299) ?
                     DM_MC02_WS2812_APPROX_BATCH : 1;

    (void)timestamp_ns;
    if (dm_mc02_tim2_get_compare_batch(timer) != batch) {
        dm_mc02_tim2_set_compare_batch(timer, batch);
    }
    /* CC1DE is a compare request.  The accurate path retains every endpoint
     * write; the approximate path keeps the final sampled CCR1 value while
     * using the DMA layer's shared state machine for NDTR/IRQ semantics. */
    if ((dier & DM_MC02_TIM8_DIER_CC1DE) && ws_dma) {
        if (s->accurate_timing) {
            (void)dm_mc02_dma_request_batch(
                ws_dma, &s->dma_subsystem.dmamux[0], s->board->tim8_request,
                s->board->tim8_ccr1, events ? events : batch);
        } else {
            (void)dm_mc02_dma_request_batch_coalesced(
                ws_dma, &s->dma_subsystem.dmamux[0], s->board->tim8_request,
                s->board->tim8_ccr1, events ? events : batch);
        }
    }
}

static void dm_mc02_publish_timer_event(
    DmMc02MachineState *s, const DmMc02TimMasterEvent *event,
    const DmMc02BoardTimerRoute *route)
{
    uint32_t source_id;
    unsigned output;
    unsigned kind;

    if (!s || !event || !route) {
        return;
    }
    output = event->output;
    kind = event->kind;
    source_id = output == DM_MC02_TIM_TRGO2 ? route->trgo2_source_id :
                                             route->trgo_source_id;
    if (output <= DM_MC02_TIM_TRGO2 &&
        kind < DM_MC02_BOARD_TIMER_EVENT_KIND_COUNT &&
        (route->master_event_source_valid[output] & (1u << kind))) {
        source_id = route->master_event_source_id[output][kind];
    }
    if (source_id == DM_MC02_TRIGGER_SOURCE_NONE) {
        return;
    }
    dm_mc02_trigger_bus_publish_batch(&s->trigger_bus, source_id,
                                      event->rising, event->event_count,
                                      event->timestamp_ns);
}

static void dm_mc02_timer_event(void *opaque,
                                const DmMc02TimMasterEvent *event)
{
    DmMc02TimerEventContext *context = opaque;
    DmMc02MachineState *s;

    if (!context || !(s = context->machine) || !s->board ||
        !context->route) {
        return;
    }
    dm_mc02_publish_timer_event(s, event, context->route);
}

static bool dm_mc02_decode_board_signals(const DmMc02MachineState *s,
                                          unsigned changed_bank,
                                          DmMc02BoardSignals *signals)
{
    DmMc02BoardGpioState gpio[DM_MC02_GPIO_BANKS];

    if (!s || !s->board || !signals ||
        s->board->soc->gpio_bank_count > ARRAY_SIZE(gpio)) {
        return false;
    }
    for (unsigned i = 0; i < s->board->soc->gpio_bank_count; ++i) {
        gpio[i].moder = s->gpio_exti.gpio[i].moder;
        gpio[i].odr = s->gpio_exti.gpio[i].odr;
        gpio[i].afr0 = s->gpio_exti.gpio[i].afr0;
        gpio[i].afr1 = s->gpio_exti.gpio[i].afr1;
    }
    return dm_mc02_board_decode_gpio_outputs(s->board, gpio,
                                             s->board->soc->gpio_bank_count,
                                             changed_bank, signals);
}

static bool dm_mc02_decode_board_spi_select(const DmMc02MachineState *s,
                                            unsigned controller,
                                            uint32_t *selected_mask)
{
    DmMc02BoardGpioState gpio[DM_MC02_GPIO_BANKS];

    if (!s || !s->board || !selected_mask ||
        s->board->soc->gpio_bank_count > ARRAY_SIZE(gpio)) {
        return false;
    }
    for (unsigned i = 0; i < s->board->soc->gpio_bank_count; ++i) {
        gpio[i].moder = s->gpio_exti.gpio[i].moder;
        gpio[i].odr = s->gpio_exti.gpio[i].odr;
        gpio[i].afr0 = s->gpio_exti.gpio[i].afr0;
        gpio[i].afr1 = s->gpio_exti.gpio[i].afr1;
    }
    return dm_mc02_board_decode_spi_selected_mask(
        s->board, gpio, s->board->soc->gpio_bank_count, controller,
        ARRAY_SIZE(s->bmi_spi_link.spi.targets), selected_mask);
}

static void dm_mc02_refresh_board_outputs(DmMc02MachineState *s,
                                          unsigned changed_bank)
{
    DmMc02BoardSignals signals = { 0 };

    if (!dm_mc02_decode_board_signals(s, changed_bank, &signals)) {
        return;
    }
    if (changed_bank == s->board->devices.power_gpio_bank) {
        dm_mc02_power_set_gpio_odr(&s->power, signals.power_gpio_odr);
        dm_mc02_apply_power_state(s);
    }
    if (s->board->devices.bmi_spi_controller == 2) {
        uint32_t selected_mask;

        if (dm_mc02_decode_board_spi_select(
                s, s->board->devices.bmi_spi_controller, &selected_mask)) {
            dm_mc02_bmi088_spi_link_select_mask(&s->bmi_spi_link,
                                                selected_mask);
        }
    }
    if (signals.led_dirty) {
        s->ws2812_dirty = true;
    }
    for (size_t i = 0; i < s->initialized_uart_count; ++i) {
        const DmMc02Rs485Signal *signal = &signals.rs485[i];
        DmMc02UartDeMode mode;

        switch (signal->mode) {
        case DM_MC02_RS485_DE_MANUAL_GPIO:
            mode = DM_MC02_UART_DE_MANUAL_GPIO;
            break;
        case DM_MC02_RS485_DE_AUTO_USART:
            mode = DM_MC02_UART_DE_AUTO_USART;
            break;
        default:
            mode = DM_MC02_UART_DE_DISCONNECTED;
            break;
        }
        if (s->board->uarts[i].rs485) {
            dm_mc02_uart_set_de_mode(&s->uart[i], mode);
            dm_mc02_uart_set_de(&s->uart[i], signal->level);
        }
    }
    dm_mc02_cosim_link_notify_telemetry(&s->cosim);
}

static void dm_mc02_timer_changed(void *opaque)
{
    DmMc02MachineState *s = opaque;

    dm_mc02_cosim_link_notify_telemetry(&s->cosim);
}

static void dm_mc02_gpio_odr_changed(void *opaque, unsigned bank,
                                      uint32_t odr)
{
    DmMc02MachineState *s = opaque;

    if (!s || !s->board) {
        return;
    }
    (void)odr;
    dm_mc02_refresh_board_outputs(s, bank);
}

/*
 * PB15 is a real board pin, so the observable buzzer state must include the
 * GPIO mux.  The timer model deliberately evaluates PWM lazily; this helper
 * keeps that property intact while presenting a board-level observation to
 * QMP and the co-sim telemetry provider.
 */
static void dm_mc02_buzzer_observe(const DmMc02MachineState *s,
                                   bool *enabled, bool *level,
                                   uint64_t *frequency_hz,
                                   uint32_t *duty_permille)
{
    const DmMc02BoardDeviceWiring *devices = &s->board->devices;
    unsigned timer_index = devices->buzzer_timer_index;
    bool timer_enabled = false;
    bool timer_level = false;
    DmMc02BoardSignals signals = { 0 };

    if (enabled) {
        *enabled = false;
    }
    if (level) {
        *level = false;
    }
    if (frequency_hz) {
        *frequency_hz = 0;
    }
    if (duty_permille) {
        *duty_permille = 0;
    }

    (void)dm_mc02_decode_board_signals(s, DM_MC02_BOARD_GPIO_BANK_COUNT,
                                       &signals);

    if (signals.buzzer_af_active && timer_index < s->initialized_timer_count) {
        (void)dm_mc02_tim2_get_pwm_state(&s->tim_aux[timer_index],
                                         devices->buzzer_channel,
                                         &timer_enabled, &timer_level,
                                         frequency_hz, duty_permille);
        if (enabled) {
            *enabled = timer_enabled;
        }
        if (level) {
            *level = timer_enabled && timer_level;
        }
    } else if (signals.buzzer_gpio_output && signals.buzzer_gpio_high) {
        /* A direct GPIO high is useful for board bring-up, but it has no
         * PWM frequency.  Keep it observable without confusing an ODR write
         * made while PB15 is still in input/alternate mode. */
        if (enabled) {
            *enabled = true;
        }
        if (level) {
            *level = true;
        }
        if (duty_permille) {
            *duty_permille = 1000;
        }
    }
}

static bool dm_mc02_get_buzzer_enabled(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    bool enabled;

    (void)errp;
    dm_mc02_buzzer_observe(s, &enabled, NULL, NULL, NULL);
    return enabled;
}

static bool dm_mc02_get_buzzer_level(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    bool level;

    (void)errp;
    dm_mc02_buzzer_observe(s, NULL, &level, NULL, NULL);
    return level;
}

static char *dm_mc02_get_buzzer_frequency(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    uint64_t frequency_hz;

    (void)errp;
    dm_mc02_buzzer_observe(s, NULL, NULL, &frequency_hz, NULL);
    return g_strdup_printf("%" PRIu64, frequency_hz);
}

static char *dm_mc02_get_buzzer_duty(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);
    uint32_t duty_permille;

    (void)errp;
    dm_mc02_buzzer_observe(s, NULL, NULL, NULL, &duty_permille);
    return g_strdup_printf("%u", duty_permille);
}

static char *dm_mc02_get_timer_output(const DmMc02MachineState *s,
                                      unsigned timer_index,
                                      Error **errp)
{
    DmMc02TimPwmOutputState output;

    (void)errp;
    if (!s || timer_index >= s->initialized_timer_count ||
        !dm_mc02_tim2_get_pwm_output_state(&s->tim_aux[timer_index], 1,
                                           &output)) {
        return g_strdup("unsupported");
    }
    return g_strdup_printf(
        "main_enabled=%u,main_level=%u,n_enabled=%u,n_level=%u"
        ",dead_time_ticks=%u,dead_time_cycles=%u,dead_time_ns=%" PRIu64,
        output.main_enabled, output.main_level,
        output.complementary_enabled, output.complementary_level,
        output.dead_time_ticks, output.dead_time_cycles, output.dead_time_ns);
}

static char *dm_mc02_get_tim1_ch1_output(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    return dm_mc02_get_timer_output(s, s->board->devices.tim1_index, errp);
}

static char *dm_mc02_get_tim8_ch1_output(Object *obj, Error **errp)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    return dm_mc02_get_timer_output(s, s->board->devices.tim8_index, errp);
}

static uint32_t dm_mc02_adc_common_status(void *opaque)
{
    return dm_mc02_adc_status(opaque);
}

static void dm_mc02_adc_common_regular_sample(
    void *opaque, unsigned adc_index, const DmMc02AdcRegularSample *sample)
{
    DmMc02MachineState *s = opaque;

    if (!s || !sample) {
        return;
    }
    (void)dm_mc02_adc_common_submit_regular_sample(
        &s->adc12_common, adc_index, sample->conversion_id, sample->rank,
        sample->value, sample->timestamp_ns);
}

static bool dm_mc02_adc_common_cdr_dma_read(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    (void)timestamp_ns;
    return dm_mc02_adc_common_cdr_read_consuming(opaque, data, size);
}

static bool dm_mc02_adc_common_cdr2_dma_read(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    (void)timestamp_ns;
    return dm_mc02_adc_common_cdr2_read_consuming(opaque, data, size);
}

static DmMc02DmaEndpointResult dm_mc02_adc_common_cdr_dma_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    (void)timestamp_ns;
    return dm_mc02_adc_common_cdr_read_prepare(opaque, data, size) ?
        DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
}

static void dm_mc02_adc_common_cdr_dma_read_commit(void *opaque)
{
    dm_mc02_adc_common_cdr_read_commit(opaque);
}

static void dm_mc02_adc_common_cdr_dma_read_abort(void *opaque)
{
    dm_mc02_adc_common_cdr_read_abort(opaque);
}

static DmMc02DmaEndpointResult dm_mc02_adc_common_cdr2_dma_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    (void)timestamp_ns;
    return dm_mc02_adc_common_cdr2_read_prepare(opaque, data, size) ?
        DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
}

static void dm_mc02_adc_common_cdr2_dma_read_commit(void *opaque)
{
    dm_mc02_adc_common_cdr2_read_commit(opaque);
}

static void dm_mc02_adc_common_cdr2_dma_read_abort(void *opaque)
{
    dm_mc02_adc_common_cdr2_read_abort(opaque);
}

static void dm_mc02_adc_common_submit_cdr_dma(DmMc02MachineState *s,
                                               uint64_t timestamp_ns)
{
    hwaddr cdr_address;

    if (!s || !s->board || !dm_mc02_adc_regular_dma_enabled(&s->adc[0]) ||
        !dm_mc02_adc_common_cdr_dma_request_pending(&s->adc12_common)) {
        return;
    }
    cdr_address = s->board->soc->adc_common_base +
                  DM_MC02_ADC_COMMON_CDR_OFFSET;
    if (s->adc_dma_endpoint) {
        DmMc02DmaEndpoint endpoint = {
            /* FIFO uses the explicitly consuming compatibility callback;
             * direct P2M selects the reservation tuple below. */
            .read = dm_mc02_adc_common_cdr_dma_read,
            .read_prepare = dm_mc02_adc_common_cdr_dma_read_prepare,
            .read_commit = dm_mc02_adc_common_cdr_dma_read_commit,
            .read_abort = dm_mc02_adc_common_cdr_dma_read_abort,
            .opaque = &s->adc12_common,
        };

        (void)dm_mc02_dma_request_endpoint(
            &s->dma_subsystem.dma[0], &s->dma_subsystem.dmamux[0],
            s->board->adc1_request, cdr_address, &endpoint, timestamp_ns);
    } else {
        /* The MMIO compatibility path consumes CDR before its destination
         * write.  It remains outside the direct source-reservation contract. */
        (void)dm_mc02_dma_request(&s->dma_subsystem.dma[0],
                                  &s->dma_subsystem.dmamux[0],
                                  s->board->adc1_request, cdr_address);
    }
}

static void dm_mc02_adc_common_data_ready(void *opaque, uint32_t data,
                                           uint64_t timestamp_ns)
{
    DmMc02MachineState *s = opaque;

    (void)data;
    dm_mc02_adc_common_submit_cdr_dma(s, timestamp_ns);
}

static void dm_mc02_adc_common_submit_cdr2_dma(DmMc02MachineState *s,
                                                uint64_t timestamp_ns)
{
    hwaddr cdr2_address;

    if (!s || !s->board || !dm_mc02_adc_regular_dma_enabled(&s->adc[0]) ||
        !dm_mc02_adc_common_cdr2_data_pending(&s->adc12_common)) {
        return;
    }
    cdr2_address = s->board->soc->adc_common_base +
                   DM_MC02_ADC_COMMON_CDR2_OFFSET;
    if (s->adc_dma_endpoint) {
        DmMc02DmaEndpoint endpoint = {
            .read = dm_mc02_adc_common_cdr2_dma_read,
            .read_prepare = dm_mc02_adc_common_cdr2_dma_read_prepare,
            .read_commit = dm_mc02_adc_common_cdr2_dma_read_commit,
            .read_abort = dm_mc02_adc_common_cdr2_dma_read_abort,
            .opaque = &s->adc12_common,
        };

        (void)dm_mc02_dma_request_endpoint(
            &s->dma_subsystem.dma[0], &s->dma_subsystem.dmamux[0],
            s->board->adc1_request, cdr2_address, &endpoint, timestamp_ns);
    } else {
        /* The MMIO compatibility path remains an immediately consuming
         * consumer and is intentionally outside this reservation boundary. */
        (void)dm_mc02_dma_request(&s->dma_subsystem.dma[0],
                                  &s->dma_subsystem.dmamux[0],
                                  s->board->adc1_request, cdr2_address);
    }
}

static void dm_mc02_adc_common_cdr2_data_ready(void *opaque, uint32_t data,
                                                unsigned source,
                                                uint64_t timestamp_ns)
{
    DmMc02MachineState *s = opaque;

    /* CDR2 data is produced by either ADC in interleaved mode.  The source
     * is retained by the common component and is consumed by its read-ack
     * callback; this routing boundary only submits the 32-bit beat. */
    (void)data;
    (void)source;
    dm_mc02_adc_common_submit_cdr2_dma(s, timestamp_ns);
}

static void dm_mc02_adc_common_dma_stream_enabled(DmMc02MachineState *s)
{
    if (!s || !s->adc_dma_endpoint ||
        !dm_mc02_adc_regular_dma_enabled(&s->adc[0]) ||
        (!dm_mc02_adc_common_cdr_data_pending(&s->adc12_common) &&
         !dm_mc02_adc_common_cdr2_data_pending(&s->adc12_common))) {
        return;
    }
    /* CDR is level-like while its single producer word remains pending.  A
     * direct DMA destination error aborts, rather than acknowledges, that
     * word; a later stream enable can therefore retry without new ADC input. */
    dm_mc02_adc_common_submit_cdr_dma(s,
                                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    dm_mc02_adc_common_submit_cdr2_dma(s,
                                       qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static void dm_mc02_adc_common_cdr_read(void *opaque)
{
    DmMc02MachineState *s = opaque;

    if (!s) {
        return;
    }
    /* RM0468: a CDR read acknowledges both regular EOC flags. */
    dm_mc02_adc_acknowledge_regular_data(&s->adc[0]);
    dm_mc02_adc_acknowledge_regular_data(&s->adc[1]);
}

static void dm_mc02_adc_common_cdr2_read(void *opaque, unsigned source)
{
    DmMc02MachineState *s = opaque;

    if (!s || source > 1) {
        return;
    }
    /* CDR2.RDATA_ALT is one result at a time; only the ADC that produced the
     * currently published value has its EOC consumed. */
    dm_mc02_adc_acknowledge_regular_data(&s->adc[source]);
}

static bool dm_mc02_adc_common_start_peer(void *opaque, unsigned source,
                                           uint64_t conversion_id)
{
    DmMc02MachineState *s = opaque;
    uint64_t start_ns;
    uint64_t now;
    unsigned delay_code;

    if (!s || source != 1) {
        return false;
    }
    if (!dm_mc02_adc_enabled(&s->adc[1])) {
        return false;
    }
    /* Seed both independent ADC kernels before starting the slave.  The
     * master start returns through the common admission path and then uses
     * the same ID; CONT sequences advance the seeded counters together. */
    dm_mc02_adc_set_regular_conversion_id(&s->adc[0], conversion_id);
    dm_mc02_adc_set_regular_conversion_id(&s->adc[1], conversion_id);
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!dm_mc02_adc_common_regular_interleaved(&s->adc12_common)) {
        dm_mc02_adc_start(&s->adc[1]);
        return true;
    }

    delay_code = dm_mc02_adc_common_get_delay_code(&s->adc12_common);
    start_ns = dm_mc02_adc_interleaved_slave_start_ns(
        &s->adc[0], now, delay_code);
    /* A stopped ADC kernel clock cannot advance the interleaved phase.  Arm
     * the slave at the current epoch; its own clock callback will keep the
     * conversion paused until a clock is restored. */
    if (start_ns == UINT64_MAX) {
        start_ns = now;
    }
    return dm_mc02_adc_start_regular_at(&s->adc[1], conversion_id, start_ns);
}

static bool dm_mc02_adc_common_external_trigger_peer(
    void *opaque, unsigned source, uint32_t trigger_source, bool rising,
    unsigned event_count, uint64_t timestamp_ns, uint64_t conversion_id)
{
    DmMc02MachineState *s = opaque;
    uint64_t conversion_start_ns = timestamp_ns;
    unsigned delay_code;

    if (!s || source != 1 || !dm_mc02_adc_enabled(&s->adc[1])) {
        return false;
    }
    /* ADC1 owns the common trigger admission.  Start ADC2 through the
     * explicit peer path so the second trigger-bus sink cannot create an
     * independent sequence or allocate another conversion ID. */
    if (dm_mc02_adc_common_regular_interleaved(&s->adc12_common)) {
        delay_code = dm_mc02_adc_common_get_delay_code(&s->adc12_common);
        conversion_start_ns = dm_mc02_adc_interleaved_slave_start_ns(
            &s->adc[0], timestamp_ns, delay_code);
        if (conversion_start_ns == UINT64_MAX) {
            conversion_start_ns = timestamp_ns;
        }
    }
    return dm_mc02_adc_external_trigger_with_id_at(
        &s->adc[1], trigger_source, rising, event_count, timestamp_ns,
        conversion_start_ns, conversion_id);
}

static bool dm_mc02_adc_common_regular_trigger_request(
    void *opaque, unsigned adc_index, uint32_t trigger_source, bool rising,
    unsigned event_count, uint64_t timestamp_ns, uint64_t *conversion_id)
{
    return dm_mc02_adc_common_admit_external_trigger(
        opaque, adc_index, trigger_source, rising, event_count, timestamp_ns,
        conversion_id);
}

static bool dm_mc02_adc_common_regular_start(void *opaque,
                                              unsigned adc_index)
{
    DmMc02MachineState *s = opaque;

    return s && dm_mc02_adc_common_admit_regular_start(
        &s->adc12_common, adc_index);
}

static void dm_mc02_adc_common_clock_changed(void *opaque, uint32_t ccr);

static void dm_mc02_clock_changed(void *opaque, uint64_t hz)
{
    DmMc02MachineState *s = opaque;
    uint64_t adc_hz;
    uint64_t fdcan_hz;
    uint64_t usart16_hz;
    uint64_t usart234578_hz;
    uint64_t apb1_timer_hz;
    uint64_t apb2_timer_hz;

    /* ADC uses the independent board PLL2P path, not CPU PLL1.  The RCC
     * model returns the compatibility default until the guest configures
     * PLL2, then computes PLL2P and the board's ADC /64 prescaler. */
    if (dm_mc02_adc_common_ccr_configured(&s->adc12_common)) {
        dm_mc02_adc_common_sync_runtime(&s->adc12_common);
    } else {
        adc_hz = dm_mc02_pwr_rcc_adc_clock_hz(&s->pwr_rcc);
        dm_mc02_adc_set_clock_hz(&s->adc[0], adc_hz);
        dm_mc02_adc_set_clock_hz(&s->adc[1], adc_hz);
    }
    fdcan_hz = dm_mc02_pwr_rcc_fdcan_kernel_clock_hz(&s->pwr_rcc);
    s->fdcan_kernel_clock_hz = fdcan_hz;
    usart16_hz = dm_mc02_pwr_rcc_usart16_kernel_clock_hz(&s->pwr_rcc);
    usart234578_hz = dm_mc02_pwr_rcc_usart234578_kernel_clock_hz(&s->pwr_rcc);
    s->usart16_kernel_clock_hz = usart16_hz;
    s->usart234578_kernel_clock_hz = usart234578_hz;
    if (clock_set_hz(s->uart16_kernelclk, usart16_hz)) {
        clock_propagate(s->uart16_kernelclk);
    }
    if (clock_set_hz(s->uart234578_kernelclk, usart234578_hz)) {
        clock_propagate(s->uart234578_kernelclk);
    }
    /* First snapshot every running counter while the old clock is still
     * visible.  This preserves phase across a live RCC change.  The timer
     * callback also reschedules its next deadline, so doing this before
     * changing the APB timer clocks would leave the first post-switch period
     * based on the old frequency.  Re-run the cheap reschedule-only path
     * below after updating the clock. */
    dm_mc02_tim2_clock_changed(&s->tim2);
    for (size_t i = 0; i < s->initialized_timer_count; ++i) {
        dm_mc02_tim2_clock_changed(&s->tim_aux[i]);
    }
    apb1_timer_hz = dm_mc02_pwr_rcc_apb1_timer_clock_hz(&s->pwr_rcc);
    apb2_timer_hz = dm_mc02_pwr_rcc_apb2_timer_clock_hz(&s->pwr_rcc);
    s->apb1_timer_clock_hz = apb1_timer_hz;
    s->apb2_timer_clock_hz = apb2_timer_hz;
    /* ST LL_APB3_GRP1_PERIPH_WWDG1: WWDG1 uses PCLK3, without TIMPRE.
     * The component preserves the
     * visible counter when this RCC-derived frequency changes. */
    dm_mc02_wwdg_set_clock_hz(&s->wwdg1,
                            dm_mc02_pwr_rcc_apb3_clock_hz(&s->pwr_rcc));
    /* A zero rate is a real readiness state.  Propagate it so a disabled or
     * unavailable RCC source cannot leave timer consumers running at the
     * previous frequency. */
    clock_set_hz(s->apb1_timerclk, apb1_timer_hz);
    clock_set_hz(s->apb2_timerclk, apb2_timer_hz);
    dm_mc02_tim2_set_clock(&s->tim2, s->apb1_timerclk);
    for (size_t i = 0; i < s->initialized_timer_count; ++i) {
        Clock *timer_clock = s->board->timers[i].clock_domain ==
                             DM_MC02_BOARD_TIMER_CLOCK_APB2 ?
                             s->apb2_timerclk : s->apb1_timerclk;

        dm_mc02_tim2_set_clock(&s->tim_aux[i], timer_clock);
    }
    if (clock_set_hz(s->sysclk, hz)) {
        clock_propagate(s->sysclk);
    }
}

static uint64_t dm_mc02_adc_prescaler(uint32_t ccr)
{
    static const uint16_t dividers[] = {
        1, 2, 4, 6, 8, 10, 12, 16,
        32, 64, 128, 256,
    };
    unsigned code = (ccr >> DM_MC02_ADC_COMMON_CCR_PRESC_SHIFT) & 0xfu;

    return code < ARRAY_SIZE(dividers) ? dividers[code] : 1;
}

static void dm_mc02_adc_common_clock_changed(void *opaque, uint32_t ccr)
{
    DmMc02MachineState *s = opaque;
    uint64_t kernel_hz;
    uint64_t clock_hz;
    unsigned ckmode = (ccr >> DM_MC02_ADC_COMMON_CCR_CKMODE_SHIFT) & 3u;

    kernel_hz = dm_mc02_pwr_rcc_adc_kernel_clock_hz(&s->pwr_rcc);
    if (!kernel_hz) {
        clock_hz = 0;
    } else if (ckmode == 0) {
        clock_hz = kernel_hz / dm_mc02_adc_prescaler(ccr);
    } else {
        /* Synchronous ADC clock modes derive from HCLK.  HCLK has its own
         * HPRE divider; it must not be inferred from the CPU-only D1CPRE. */
        clock_hz = dm_mc02_pwr_rcc_hclk_hz(&s->pwr_rcc);
        if (ckmode == 2) {
            clock_hz /= 2;
        } else if (ckmode == 3) {
            clock_hz /= 4;
        }
    }
    dm_mc02_adc_set_clock_hz(&s->adc[0], clock_hz);
    dm_mc02_adc_set_clock_hz(&s->adc[1], clock_hz);
}

static void dm_mc02_machine_reset(void *opaque)
{
    DmMc02MachineState *s = opaque;
    DmMc02BoardGpioPowerReset board_reset = {
        .gpio = s->gpio_exti.gpio,
        .gpio_count = ARRAY_SIZE(s->gpio_exti.gpio),
        .syscfg = &s->gpio_exti.syscfg,
        .exti = &s->gpio_exti.exti,
        .power = &s->power,
        .reset_spi_cs = dm_mc02_reset_spi_cs_hook,
        .apply_gpio_inputs = dm_mc02_apply_gpio_inputs_hook,
        .apply_power = dm_mc02_apply_power_hook,
        .opaque = s,
    };

    if (s->cold_reset) {
        /* A cold board reset clears volatile memories but deliberately keeps
         * internal and external non-volatile Flash contents intact. */
        dm_mc02_soc_memory_reset_volatile(&s->soc_memory, s->board->soc);
    }

    /* Reset only peripheral state.  CPU/ordinary RAM reset is owned by
     * QEMU's normal reset machinery; retaining RAM matches a warm system
     * reset while clearing all board-side queues and observable devices. */
    dm_mc02_tim2_reset(&s->tim2);
    for (size_t i = 0; i < s->initialized_timer_count; ++i) {
        dm_mc02_tim2_reset(&s->tim_aux[i]);
    }
    for (size_t i = 0; i < s->initialized_uart_count; ++i) {
        dm_mc02_uart_reset(&s->uart[i]);
    }
    for (size_t i = 0; i < s->initialized_fdcan_count; ++i) {
        dm_mc02_fdcan_reset(&s->fdcan[i]);
    }
    dm_mc02_adc_reset(&s->adc[0]);
    dm_mc02_adc_reset(&s->adc[1]);
    dm_mc02_adc_common_reset(&s->adc12_common);
    dm_mc02_dma_reset(&s->dma_subsystem.dma[0]);
    dm_mc02_dma_reset(&s->dma_subsystem.dma[1]);
    dm_mc02_dmamux_reset(&s->dma_subsystem.dmamux[0]);
    dm_mc02_dmamux_reset(&s->dma_subsystem.dmamux[1]);
    /* Keep board GPIO, external input routing and switched-rail projection in
     * one composition-owned reset stage. */
    dm_mc02_board_reset_gpio_power(&board_reset);
    dm_mc02_soc_memory_reset_fdcan_msg_ram(&s->soc_memory);

    dm_mc02_bmi088_spi_link_reset(&s->bmi_spi_link);
    dm_mc02_spi_reset(&s->spi1_state);
    dm_mc02_bmi088_signal_set_rng(
        &s->bmi_spi_link.gyro.signal, s->imu_seed ^ UINT32_C(0x9e3779b9));
    dm_mc02_bmi088_signal_set_rng(
        &s->bmi_spi_link.accel.signal, s->imu_seed ^ UINT32_C(0x243f6a88));
    s->imu_consume_step_id = 0;
    s->imu_consume_step_valid = false;

    dm_mc02_ospi_reset(&s->ospi2);
    dm_mc02_ospi_reset(&s->ospi1);
    dm_mc02_flash_reset(&s->flash_regs);
    dm_mc02_rng_reset(&s->rng);
    dm_mc02_crc_reset(&s->crc);
    dm_mc02_fmc_reset(&s->fmc);
    dm_mc02_dbgmcu_reset(&s->dbgmcu);
    dm_mc02_cordic_reset(&s->cordic);
    dm_mc02_pwr_rcc_reset(&s->pwr_rcc);
    dm_mc02_reset_input_reset(&s->reset_input);
    dm_mc02_usb_reset(&s->usb_hs);

    memset(s->ws2812_led_rgb, 0, sizeof(s->ws2812_led_rgb));
    s->ws2812_dirty = true;
    if (s->ws2812_timer) {
        timer_del(s->ws2812_timer);
        if (s->cosim.enabled) {
            timer_mod(s->ws2812_timer,
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10 * 1000 * 1000);
        }
    }
    dm_mc02_cosim_link_reset(&s->cosim);
    s->cosim_motor_loopback_steps = 0;
}

static uint32_t dm_mc02_dma_reg(const DmMc02Dma *dma, hwaddr offset)
{
    return ldl_le_p(dma->regs + offset);
}

static uint8_t dm_mc02_ws2812_byte(const uint8_t *waveform,
                                   unsigned bit_offset)
{
    uint8_t value = 0;

    for (unsigned bit = 0; bit < 8; ++bit) {
        uint16_t duty = lduw_le_p(waveform + 2 * (bit_offset + bit));

        value = (uint8_t)((value << 1) |
                          (duty >= DM_MC02_WS2812_THRESHOLD ? 1 : 0));
    }
    return value;
}

/*
 * Observe the buffer owned by the board profile's WS2812 DMA stream.  The DMA
 * engine does not call into this routine: firmware may update the memory
 * while all DMA registers remain unchanged, so the machine samples it from a
 * low-rate virtual timer and lets the co-sim link's existing value de-duper
 * suppress unchanged telemetry.
 */
static bool dm_mc02_ws2812_decode(DmMc02MachineState *s,
                                  uint32_t *first_rgb,
                                  uint32_t *first_brightness)
{
    const DmMc02BoardDeviceWiring *devices = &s->board->devices;
    DmMc02Dma *ws_dma = dm_mc02_board_dma(s,
                                          devices->ws2812_dma_controller);
    const hwaddr stream = DM_MC02_DMA_STREAM_BASE +
                          devices->ws2812_dma_stream *
                          DM_MC02_DMA_STREAM_STRIDE;
    const unsigned waveform_bytes = DM_MC02_WS2812_TOTAL_BITS * 2;
    uint32_t cr;
    uint32_t ndtr;
    dma_addr_t m0ar;
    uint8_t waveform[DM_MC02_WS2812_TOTAL_BITS * 2] = { 0 };
    uint8_t green, red, blue;
    size_t transfer_bytes;
    MemTxResult result;

    if (!ws_dma) {
        return false;
    }
    cr = dm_mc02_dma_reg(ws_dma, stream + DM_MC02_DMA_SxCR);
    ndtr = dm_mc02_dma_reg(ws_dma, stream + DM_MC02_DMA_SxNDTR);
    m0ar = dm_mc02_dma_reg(ws_dma, stream + DM_MC02_DMA_SxM0AR);

    /* The current board buffer is exactly 72 half-word transfers.  Permit a
     * shorter transfer only when it still contains one complete LED, which
     * is useful while firmware is bringing the stream up. */
    if (!(cr & DM_MC02_DMA_CR_EN) || !(cr & DM_MC02_DMA_CR_MINC) ||
        ((cr >> DM_MC02_DMA_CR_PSIZE_SHIFT) & 3u) != DM_MC02_DMA_SIZE_16 ||
        ((cr >> DM_MC02_DMA_CR_MSIZE_SHIFT) & 3u) != DM_MC02_DMA_SIZE_16 ||
        ndtr < DM_MC02_WS2812_FIRST_BITS ||
        ndtr > DM_MC02_WS2812_TOTAL_BITS ||
        m0ar < s->board->soc->d2_sram_base ||
        m0ar > s->board->soc->d2_sram_base +
                s->board->soc->d2_sram_size - waveform_bytes ||
        (m0ar & 1u)) {
        return false;
    }

    transfer_bytes = (size_t)ndtr * sizeof(uint16_t);
    result = dma_memory_read(&address_space_memory, m0ar, waveform,
                             transfer_bytes, MEMTXATTRS_UNSPECIFIED);
    if (result != MEMTX_OK) {
        return false;
    }

    green = dm_mc02_ws2812_byte(waveform, 0);
    red = dm_mc02_ws2812_byte(waveform, 8);
    blue = dm_mc02_ws2812_byte(waveform, 16);
    s->ws2812_led_rgb[0] = ((uint32_t)red << 16) |
                           ((uint32_t)green << 8) | blue;

    /* Keep the second pixel decoded for a future multi-pixel telemetry
     * payload, while the v1 protocol continues to expose the first pixel. */
    if (ndtr >= 48) {
        green = dm_mc02_ws2812_byte(waveform, 24);
        red = dm_mc02_ws2812_byte(waveform, 32);
        blue = dm_mc02_ws2812_byte(waveform, 40);
        s->ws2812_led_rgb[1] = ((uint32_t)red << 16) |
                               ((uint32_t)green << 8) | blue;
    } else {
        s->ws2812_led_rgb[1] = 0;
    }

    *first_rgb = s->ws2812_led_rgb[0];
    *first_brightness = MAX(((*first_rgb >> 16) & 0xff),
                            MAX((*first_rgb >> 8) & 0xff, *first_rgb & 0xff));
    return true;
}

static void dm_mc02_ws2812_refresh(void *opaque)
{
    DmMc02MachineState *s = opaque;
    int64_t next = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10 * 1000 * 1000;

    s->ws2812_dirty = true;
    dm_mc02_cosim_link_notify_telemetry(&s->cosim);
    timer_mod(s->ws2812_timer, next);
}

static void dm_mc02_cosim_telemetry(void *opaque,
                                    DmMc02CosimTelemetry *telemetry)
{
    DmMc02MachineState *s = opaque;
    const DmMc02BoardDeviceWiring *devices = &s->board->devices;
    bool led_active = dm_mc02_board_pin_high(s, devices->led);
    bool buzzer_enabled;
    uint64_t buzzer_frequency_hz;
    uint32_t buzzer_duty_permille;
    uint32_t led_rgb;
    uint32_t led_brightness;

    if (s->ws2812_dirty) {
        if (dm_mc02_ws2812_decode(s, &led_rgb, &led_brightness)) {
            s->ws2812_led_rgb[0] = led_rgb;
        } else {
            /* PA7 remains useful during early GPIO-only bring-up. */
            s->ws2812_led_rgb[0] = led_active ? UINT32_C(0xffffff) : 0;
        }
        s->ws2812_dirty = false;
    }
    telemetry->led_rgb = s->ws2812_led_rgb[0];
    telemetry->led_brightness = MAX((telemetry->led_rgb >> 16) & 0xff,
                                    MAX((telemetry->led_rgb >> 8) & 0xff,
                                        telemetry->led_rgb & 0xff));
    /* Keep the v1 wire field as an enabled/non-zero-output indication; QOM
     * also exposes instantaneous PWM state without per-edge timers. */
    dm_mc02_buzzer_observe(s, &buzzer_enabled, NULL, &buzzer_frequency_hz,
                           &buzzer_duty_permille);
    telemetry->buzzer = buzzer_enabled;
    telemetry->buzzer_frequency_hz = (uint32_t)MIN(buzzer_frequency_hz,
                                                   UINT32_MAX);
    telemetry->buzzer_duty_permille = buzzer_duty_permille;
    /* bit 0 = board power output 2 enable, bit 1 = switched 5 V enable. */
    telemetry->board_flags = dm_mc02_board_pin_high(s, devices->power_out2) |
                             (dm_mc02_board_pin_high(s, devices->power_5v) << 1);
}

static void dm_mc02_cosim_imu(void *opaque, const float gyro[3],
                              const float accel[3], uint64_t sample_sequence,
                              uint64_t step_id,
                              uint64_t sensor_time_ns)
{
    DmMc02MachineState *s = opaque;

    s->last_imu_sequence = sample_sequence;
    s->last_imu_time_ns = sensor_time_ns;
    s->imu_consume_step_id = step_id;
    s->imu_consume_step_valid = step_id != 0;
    dm_mc02_bmi088_sample(&s->bmi_spi_link.gyro, gyro, sample_sequence,
                          sensor_time_ns,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    dm_mc02_bmi088_sample(&s->bmi_spi_link.accel, accel, sample_sequence,
                          sensor_time_ns,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static void dm_mc02_cosim_adc(void *opaque, uint16_t channel, uint16_t raw)
{
    DmMc02MachineState *s = opaque;

    /* ADC_INPUT is an external raw sample source; the ADC model applies it
     * when the firmware-selected SQR1 rank reaches this channel. */
    dm_mc02_adc_set_channel_raw(&s->adc[0], channel, raw);
}

static void dm_mc02_cosim_adc_pin_voltage(void *opaque, uint16_t channel,
                                          uint16_t flags, uint32_t voltage_uv)
{
    DmMc02MachineState *s = opaque;

    /* Both flag values accepted by the link describe an external pin source;
     * bit 0 only makes the override intent explicit on the wire. */
    (void)flags;
    dm_mc02_adc_set_channel_pin_voltage_uv(&s->adc[0], channel, voltage_uv);
}

static void dm_mc02_init(MachineState *machine)
{
    DmMc02MachineState *s = (DmMc02MachineState *)machine;
    const DmMc02BoardProfile *board = s->board;
    const DmMc02SocProfile *soc = board->soc;
    MemoryRegion *system_memory = get_system_memory();
    DeviceState *armv7m;

    if (!dm_mc02_board_validate(board, &error_fatal)) {
        return;
    }
    s->board = board;
    s->board_profile_locked = true;
    if (!s->canbus) {
        Object *canbus = object_new(TYPE_CAN_BUS);

        object_property_add_child(OBJECT(machine), "canbus-internal", canbus);
        s->canbus = CAN_BUS(canbus);
        object_unref(canbus);
    }
    dm_mc02_trigger_bus_init(&s->trigger_bus);
    g_assert(soc != NULL);
    s->sysclk = clock_new(OBJECT(machine), "SYSCLK");
    /* Reset selects HSI.  RCC will update this clock when the guest enables
     * PLL1 or changes the system-clock source/prescaler. */
    clock_set_hz(s->sysclk, soc->reset_cpu_hz);
    s->apb1_timerclk = clock_new(OBJECT(machine), "APB1_TIMERCLK");
    s->apb2_timerclk = clock_new(OBJECT(machine), "APB2_TIMERCLK");
    /* D2CFGR reset selects APB /1, so TIMPRE=0 gives HCLK at reset. */
    clock_set_hz(s->apb1_timerclk, soc->reset_cpu_hz);
    clock_set_hz(s->apb2_timerclk, soc->reset_cpu_hz);
    s->uart16_kernelclk = clock_new(OBJECT(machine), "USART16_KERNELCLK");
    s->uart234578_kernelclk = clock_new(OBJECT(machine),
                                        "USART234578_KERNELCLK");
    s->refclk = clock_new(OBJECT(machine), "REFCLK");
    clock_set_hz(s->refclk, soc->refclk_hz);

    /* The SoC owns its internal memory map; the machine adds the flash
     * programming window and optional persistent backing below. */
    dm_mc02_soc_memory_init(&s->soc_memory, OBJECT(machine), system_memory,
                            soc, &error_fatal);
    if (s->flash_file && s->flash_file[0]) {
        DmNorFlashPersistenceResult result = dm_nor_flash_persistence_load(
            s->flash_file,
            memory_region_get_ram_ptr(&s->soc_memory.flash),
            soc->flash_size);

        if (result != DM_NOR_FLASH_PERSISTENCE_OK &&
            result != DM_NOR_FLASH_PERSISTENCE_NOT_FOUND) {
            error_report("DM-MC02 internal Flash image '%s' rejected "
                         "(result=%d; expected an exact %u-byte raw image)",
                         s->flash_file, result, soc->flash_size);
            exit(EXIT_FAILURE);
        }
    }

    dm_mc02_flash_init(&s->flash_regs, OBJECT(machine),
                       memory_region_get_ram_ptr(&s->soc_memory.flash),
                       soc->flash_size, &s->soc_memory.flash);
    memory_region_add_subregion_overlap(system_memory, soc->flash_base,
                                         &s->flash_regs.program_window, 1);
    memory_region_add_subregion(system_memory, soc->flash_r_base,
                                &s->flash_regs.iomem);

    dm_mc02_fmc_init(&s->fmc, OBJECT(machine));
    memory_region_add_subregion(system_memory, soc->fmc_r_base,
                                &s->fmc.iomem);

    dm_mc02_tim2_init(&s->tim2, OBJECT(machine), dm_mc02_timer_changed, s);
    dm_mc02_tim2_set_clock(&s->tim2, s->apb1_timerclk);
    s->tim2_event_context = (DmMc02TimerEventContext){
        .machine = s,
        .route = &board->tim2,
    };
    dm_mc02_tim2_set_event_callback(
        &s->tim2, dm_mc02_timer_event, &s->tim2_event_context);
    memory_region_add_subregion(system_memory, soc->tim2_base,
                                &s->tim2.iomem);

    /* These timers share the minimal TIM2 register behavior. Keep each
     * instance in machine state so its region lives with the machine. */
    for (size_t i = 0; i < board->timer_count; ++i) {
        dm_mc02_tim2_init(&s->tim_aux[i], OBJECT(machine),
                          i == board->devices.buzzer_timer_index ?
                          dm_mc02_timer_changed : NULL, s);
        dm_mc02_tim2_set_clock(
            &s->tim_aux[i], board->timers[i].clock_domain ==
                             DM_MC02_BOARD_TIMER_CLOCK_APB2 ?
                             s->apb2_timerclk : s->apb1_timerclk);
        dm_mc02_tim2_set_repetition_supported(
            &s->tim_aux[i], board->timers[i].has_repetition_counter);
        dm_mc02_tim2_set_break_supported(
            &s->tim_aux[i], board->timers[i].has_break_output);
        dm_mc02_tim2_set_complementary_supported(
            &s->tim_aux[i], board->timers[i].has_complementary_output);
        if (i == board->devices.tim8_index || i == board->devices.tim1_index) {
            dm_mc02_tim2_set_mms2_supported(&s->tim_aux[i], true);
        }
        memory_region_add_subregion(system_memory, board->timers[i].base,
                                    &s->tim_aux[i].iomem);
        s->timer_event_context[i] = (DmMc02TimerEventContext){
            .machine = s,
            .route = &board->timers[i],
        };
        dm_mc02_tim2_set_event_callback(
            &s->tim_aux[i], dm_mc02_timer_event,
            &s->timer_event_context[i]);
        s->initialized_timer_count++;
    }
    dm_mc02_tim2_set_update_callback(
        &s->tim_aux[board->devices.tim8_index], dm_mc02_tim8_update, s);
    dm_mc02_tim2_set_compare_callback(
        &s->tim_aux[board->devices.tim8_index], dm_mc02_tim8_compare, s);

    dm_mc02_syscfg_init(&s->gpio_exti.syscfg, OBJECT(machine));
    dm_mc02_syscfg_set_changed(&s->gpio_exti.syscfg,
                               dm_mc02_syscfg_changed, s);
    memory_region_add_subregion(system_memory, soc->syscfg_base,
                                &s->gpio_exti.syscfg.iomem);

    dm_mc02_exti_init(&s->gpio_exti.exti, OBJECT(machine));
    memory_region_add_subregion(system_memory, soc->exti_base,
                                &s->gpio_exti.exti.iomem);

    for (unsigned i = 0; i < board->uart_count; ++i) {
        const DmMc02BoardUartRoute *route = &board->uarts[i];
        static const char * const uart_kernel_clock_names[] = {
            "UART0_KERNELCLK", "UART1_KERNELCLK", "UART2_KERNELCLK",
            "UART3_KERNELCLK", "UART4_KERNELCLK", "UART5_KERNELCLK",
        };
        Clock *kernel_source = route->kernel_clock_group ==
                               DM_MC02_UART_KERNEL_CLOCK_USART16 ?
                               s->uart16_kernelclk :
                               s->uart234578_kernelclk;

        /* serial_hd(0) is reserved for the binary co-sim link. Additional
         * -serial backends, when supplied, map in board UART order. */
        if (!dm_mc02_uart_init(&s->uart[i], OBJECT(machine), route->name,
                               serial_hd(route->serial_slot), &error_fatal)) {
            return;
        }
        memory_region_add_subregion(system_memory, route->base,
                                    &s->uart[i].iomem);
        s->uart_kernelclk[i] = clock_new(OBJECT(machine),
                                         uart_kernel_clock_names[i]);
        clock_set_source(s->uart_kernelclk[i], kernel_source);
        dm_mc02_uart_set_kernel_clock(&s->uart[i], s->uart_kernelclk[i]);
        if (route->rs485) {
            dm_mc02_uart_set_rs485(&s->uart[i], true, true);
        }
        s->initialized_uart_count++;
    }
    for (size_t i = 0; i < s->initialized_uart_count; ++i) {
        dm_mc02_uart_set_dma_endpoint(&s->uart[i], s->uart_dma_endpoint);
    }

    uint8_t *fdcan_msg_ram = dm_message_ram_data(
        &s->soc_memory.fdcan_msg_ram);
    for (unsigned i = 0; i < board->fdcan_count; ++i) {
        const DmMc02BoardFdcanRoute *route = &board->fdcans[i];

        /* serial_hd(0) is co-sim, 1..6 are UARTs, 7..9 are FDCAN links. */
        if (!dm_mc02_fdcan_init(&s->fdcan[i], OBJECT(machine), route->name,
                                route->region_size, fdcan_msg_ram,
                                soc->fdcan_msg_ram_size,
                                serial_hd(route->serial_slot),
                                &error_fatal)) {
            return;
        }
        dm_mc02_fdcan_set_canbus(&s->fdcan[i], s->canbus);
        dm_mc02_fdcan_set_host_ack(&s->fdcan[i], s->fdcan_host_ack);
        memory_region_add_subregion(system_memory, route->base,
                                    &s->fdcan[i].iomem);
        s->initialized_fdcan_count++;
    }

    dm_mc02_adc_init(&s->adc[0], OBJECT(machine), "dm-mc02.adc1");
    memory_region_add_subregion(system_memory, soc->adc1_base,
                                &s->adc[0].iomem);
    dm_mc02_adc_init(&s->adc[1], OBJECT(machine), "dm-mc02.adc2");
    memory_region_add_subregion(system_memory, soc->adc2_base,
                                &s->adc[1].iomem);
    dm_mc02_adc_set_accurate_timing(&s->adc[0], s->adc_accurate_timing);
    dm_mc02_adc_set_accurate_timing(&s->adc[1], s->adc_accurate_timing);
    dm_mc02_adc_set_power_model(&s->adc[0], s->adc_power_model);
    dm_mc02_adc_set_power_model(&s->adc[1], s->adc_power_model);
    dm_mc02_adc_common_init(&s->adc12_common, OBJECT(machine),
                            "dm-mc02.adc12-common");
    memory_region_add_subregion(system_memory, soc->adc_common_base,
                                &s->adc12_common.iomem);
    dm_mc02_adc_common_set_status_sources(
        &s->adc12_common, dm_mc02_adc_common_status, &s->adc[0],
        dm_mc02_adc_common_status, &s->adc[1]);
    dm_mc02_adc_common_set_clock_callback(
        &s->adc12_common, dm_mc02_adc_common_clock_changed, s);
    dm_mc02_adc_common_set_data_ready_callback(
        &s->adc12_common, dm_mc02_adc_common_data_ready, s);
    dm_mc02_adc_common_set_cdr2_data_ready_callback(
        &s->adc12_common, dm_mc02_adc_common_cdr2_data_ready, s);
    dm_mc02_adc_common_set_cdr_read_callback(
        &s->adc12_common, dm_mc02_adc_common_cdr_read, s);
    dm_mc02_adc_common_set_cdr2_read_callback(
        &s->adc12_common, dm_mc02_adc_common_cdr2_read, s);
    dm_mc02_adc_common_set_regular_start_peer(
        &s->adc12_common, dm_mc02_adc_common_start_peer, s);
    dm_mc02_adc_common_set_external_trigger_peer(
        &s->adc12_common, dm_mc02_adc_common_external_trigger_peer, s);
    dm_mc02_adc_set_regular_sample_callback(
        &s->adc[0], 0, dm_mc02_adc_common_regular_sample, s);
    dm_mc02_adc_set_regular_sample_callback(
        &s->adc[1], 1, dm_mc02_adc_common_regular_sample, s);
    dm_mc02_adc_set_regular_start_callback(
        &s->adc[0], 0, dm_mc02_adc_common_regular_start, s);
    dm_mc02_adc_set_regular_start_callback(
        &s->adc[1], 1, dm_mc02_adc_common_regular_start, s);
    dm_mc02_adc_set_regular_trigger_callback(
        &s->adc[0], 0, dm_mc02_adc_common_regular_trigger_request,
        &s->adc12_common);
    dm_mc02_adc_set_regular_trigger_callback(
        &s->adc[1], 1, dm_mc02_adc_common_regular_trigger_request,
        &s->adc12_common);
    g_assert(dm_mc02_trigger_bus_connect(
        &s->trigger_bus, dm_mc02_adc_trigger_sink, &s->adc[0]));
    g_assert(dm_mc02_trigger_bus_connect(
        &s->trigger_bus, dm_mc02_adc_trigger_sink, &s->adc[1]));
    dm_mc02_dma_subsystem_set_identity(&s->dma_subsystem);
    dm_mc02_dma_init(&s->dma_subsystem.dma[0], OBJECT(machine),
                     "dm-mc02.dma1");
    memory_region_add_subregion(system_memory, soc->dma1_base,
                                &s->dma_subsystem.dma[0].iomem);
    dm_mc02_dma_init(&s->dma_subsystem.dma[1], OBJECT(machine),
                     "dm-mc02.dma2");
    memory_region_add_subregion(system_memory, soc->dma2_base,
                                &s->dma_subsystem.dma[1].iomem);
    dm_mc02_dmamux_init(&s->dma_subsystem.dmamux[0], OBJECT(machine),
                        "dm-mc02.dmamux1");
    memory_region_add_subregion(system_memory, soc->dmamux1_base,
                                &s->dma_subsystem.dmamux[0].iomem);
    /* Keep the second DMAMUX window for a future BDMA slice.  H723 DMA1 and
     * DMA2 both use DMAMUX1; DMA2's channel offset is configured below. */
    dm_mc02_dmamux_init(&s->dma_subsystem.dmamux[1], OBJECT(machine),
                        "dm-mc02.dmamux2");
    memory_region_add_subregion(system_memory, soc->dmamux2_base,
                                &s->dma_subsystem.dmamux[1].iomem);

    /* ADC1 uses DMAMUX1 request 9 on the board.  The firmware selects the
     * concrete DMA1 stream and DMAMUX channel; the ADC only supplies the
     * request and its fixed data-register endpoint. */
    dm_mc02_adc_set_dma(&s->adc[0], &s->dma_subsystem.dma[0],
                        &s->dma_subsystem.dmamux[0],
                        board->adc1_request, board->adc1_dr);
    dm_mc02_adc_set_dma_endpoint(&s->adc[0], s->adc_dma_endpoint);
    dm_mc02_adc_set_dma_endpoint(&s->adc[1], s->adc_dma_endpoint);
    dm_mc02_adc_set_samples(&s->adc[0], 0x0100, 0x0200);
    dm_mc02_power_init(&s->power, &s->adc[0]);
    dm_mc02_power_set_wiring(
        &s->power,
        dm_mc02_board_pin_mask(s, board->devices.power_out1),
        dm_mc02_board_pin_mask(s, board->devices.power_out2),
        dm_mc02_board_pin_mask(s, board->devices.power_5v),
        board->devices.power_vin_adc_channel,
        board->devices.power_key_adc_channel);
    dm_mc02_power_set_vin_mv(&s->power, s->vin_mv);
    dm_mc02_set_electrical_power(OBJECT(s), s->electrical_power, NULL);

    /* Fixed STM32H723 UART DMA routing used by the board firmware.  Keep the
     * request IDs and stream choices explicit here so a peripheral request
     * cannot accidentally match another UART's stream. */
    for (unsigned i = 0; i < board->uart_count; ++i) {
        const DmMc02BoardUartRoute *route = &board->uarts[i];
        DmMc02Dma *rx_dma = route->rx_dma2 ?
                            &s->dma_subsystem.dma[1] :
                            &s->dma_subsystem.dma[0];
        DmMc02Dma *tx_dma = route->tx_dma2 ?
                            &s->dma_subsystem.dma[1] :
                            &s->dma_subsystem.dma[0];

        if (route->has_rx) {
            dm_mc02_uart_set_dma_rx(&s->uart[i], rx_dma,
                                    &s->dma_subsystem.dmamux[0],
                                    route->rx_request,
                                    route->base + DM_MC02_USART_RDR);
        }
        if (route->has_tx) {
            dm_mc02_uart_set_dma_tx(&s->uart[i], tx_dma,
                                    &s->dma_subsystem.dmamux[0],
                                    route->tx_request,
                                    route->base + DM_MC02_USART_TDR);
        }
    }
    dm_mc02_dma_set_dmamux_channel_offset(&s->dma_subsystem.dma[0], 0);
    dm_mc02_dma_set_dmamux_channel_offset(&s->dma_subsystem.dma[1], 8);
    dm_mc02_dma_set_stream_enabled_callback(&s->dma_subsystem.dma[0],
                                             dm_mc02_dma_stream_enabled, s);
    dm_mc02_dma_set_stream_enabled_callback(&s->dma_subsystem.dma[1],
                                             dm_mc02_uart_dma2_stream_enabled,
                                             s);

    dm_mc02_dbgmcu_init(&s->dbgmcu, OBJECT(machine));
    memory_region_add_subregion(system_memory, soc->dbgmcu_base,
                                &s->dbgmcu.iomem);

    dm_mc02_rng_init(&s->rng, OBJECT(machine));
    memory_region_add_subregion(system_memory, soc->rng_base,
                                &s->rng.iomem);

    dm_mc02_crc_init(&s->crc, OBJECT(machine));
    memory_region_add_subregion(system_memory, soc->crc_base,
                                &s->crc.iomem);

    dm_mc02_iwdg_init(&s->iwdg1, OBJECT(machine));
    dm_mc02_iwdg_set_lsi_hz(&s->iwdg1, s->iwdg_lsi_hz);
    dm_mc02_iwdg_set_lsi_error_ppm(&s->iwdg1, s->iwdg_lsi_error_ppm);
    dm_mc02_iwdg_set_boot_grace_ms(&s->iwdg1, s->iwdg_boot_grace_ms);
    memory_region_add_subregion(system_memory, soc->iwdg1_base,
                                &s->iwdg1.iomem);
    dm_mc02_wwdg_init(&s->wwdg1, OBJECT(machine),
                      dm_mc02_pwr_rcc_apb3_clock_hz(&s->pwr_rcc));
    memory_region_add_subregion(system_memory, soc->wwdg1_base,
                                &s->wwdg1.iomem);
    if (!dm_mc02_usb_init(&s->usb_hs, OBJECT(machine), serial_hd(10),
                          &error_fatal)) {
        return;
    }
    memory_region_add_subregion(system_memory, soc->usb_hs_base,
                                &s->usb_hs.iomem);

    dm_mc02_cordic_init(&s->cordic, OBJECT(machine));
    memory_region_add_subregion(system_memory, soc->cordic_base,
                                &s->cordic.iomem);

    dm_mc02_bmi088_set_rng(
        &s->bmi_spi_link.gyro, s->imu_seed ^ UINT32_C(0x9e3779b9));
    dm_mc02_bmi088_set_rng(
        &s->bmi_spi_link.accel, s->imu_seed ^ UINT32_C(0x243f6a88));
    dm_mc02_spi_init(&s->bmi_spi_link.spi, OBJECT(machine), "dm-mc02.spi2",
                     true);
    dm_mc02_spi_set_dma_channels(
        &s->bmi_spi_link.spi,
        &(DmMc02SpiDmaChannel) {
            .dma = board->devices.bmi_spi_tx_dma == 0 ?
                   &s->dma_subsystem.dma[0] :
                   &s->dma_subsystem.dma[1],
            .dmamux = &s->dma_subsystem.dmamux[0],
            .stream = board->devices.bmi_spi_tx_stream,
            .request_id = board->devices.bmi_spi_tx_request,
            .peripheral_addr = board->soc->spi2_base + DM_MC02_SPI_TXDR,
        },
        &(DmMc02SpiDmaChannel) {
            .dma = board->devices.bmi_spi_rx_dma == 0 ?
                   &s->dma_subsystem.dma[0] :
                   &s->dma_subsystem.dma[1],
            .dmamux = &s->dma_subsystem.dmamux[0],
            .stream = board->devices.bmi_spi_rx_stream,
            .request_id = board->devices.bmi_spi_rx_request,
            .peripheral_addr = board->soc->spi2_base + DM_MC02_SPI_RXDR,
        });
    dm_mc02_spi_set_dma_endpoint(&s->bmi_spi_link.spi,
                                  s->spi_dma_endpoint);
    memory_region_add_subregion(system_memory, soc->spi2_base,
                                &s->bmi_spi_link.spi.iomem);
    dm_mc02_spi_init(&s->spi1_state, OBJECT(machine), "dm-mc02.spi1", false);
    memory_region_add_subregion(system_memory, soc->spi1_base,
                                &s->spi1_state.iomem);
    static const char * const gpio_names[DM_MC02_GPIO_BANKS] = {
        "dm-mc02.gpioa", "dm-mc02.gpiob", "dm-mc02.gpioc",
        "dm-mc02.gpiod", "dm-mc02.gpioe", "dm-mc02.gpiof",
        "dm-mc02.gpiog", "dm-mc02.gpioh",
    };
    for (unsigned bank = 0; bank < DM_MC02_GPIO_BANKS; ++bank) {
        dm_mc02_gpio_init(&s->gpio_exti.gpio[bank], OBJECT(machine),
                          gpio_names[bank],
                          bank, dm_mc02_gpio_odr_changed, s);
        memory_region_add_subregion(system_memory,
                                    soc->gpio_base + bank * 0x400,
                                    &s->gpio_exti.gpio[bank].iomem);
    }
    s->gpio_exti.gpio_count = DM_MC02_GPIO_BANKS;
    dm_mc02_gpio_exti_set_input_sync(&s->gpio_exti,
                                     dm_mc02_apply_gpio_inputs_hook, s);
    s->gpio_ready = true;
    dm_mc02_apply_gpio_inputs(s);
    /* GPIO reset leaves both DE pins disconnected until firmware configures
     * AF7 or explicitly selects normal GPIO output mode. */
    dm_mc02_refresh_board_outputs(s, s->board->devices.power_gpio_bank);
    /* GPIO reset is zero; explicitly deassert both active-low BMI088 CS pins
     * through the same profile-aware path used by system reset. */
    dm_mc02_reset_spi_cs(s);

    dm_mc02_pwr_rcc_init(&s->pwr_rcc, OBJECT(machine));
    dm_mc02_reset_input_set_callback(&s->reset_input,
                                     dm_mc02_pin_reset, s);
    sysbus_realize(SYS_BUS_DEVICE(&s->reset_input), &error_fatal);
    dm_mc02_power_set_brownout_callback(&s->power,
                                        dm_mc02_power_brownout, s);
    /* The initial machine construction is the only explicit power-on event.
     * Later QMP/system resets must preserve this flag rather than creating a
     * second POR cause. */
    dm_mc02_pwr_rcc_note_power_on_reset(&s->pwr_rcc);
    dm_mc02_pwr_rcc_set_clock_callback(&s->pwr_rcc,
                                       dm_mc02_clock_changed, s);
    dm_mc02_iwdg_set_reset_callback(&s->iwdg1,
                                    dm_mc02_pwr_rcc_note_iwdg_reset,
                                    &s->pwr_rcc);
    dm_mc02_wwdg_set_reset_callback(&s->wwdg1,
                                    dm_mc02_pwr_rcc_note_wwdg_reset,
                                    &s->pwr_rcc);
    memory_region_add_subregion(system_memory, soc->rcc_base,
                                &s->pwr_rcc.rcc);
    memory_region_add_subregion(system_memory, soc->pwr_base,
                                &s->pwr_rcc.pwr);

    armv7m = qdev_new(TYPE_ARMV7M);
    object_property_add_child(OBJECT(machine), "armv7m", OBJECT(armv7m));
    /* Include the H723 high-numbered FDCAN3 and TIM24 vectors. */
    qdev_prop_set_uint32(armv7m, "num-irq", soc->irq_count);
    qdev_prop_set_string(armv7m, "cpu-type", soc->cpu_type);
    qdev_prop_set_uint32(armv7m, "init-nsvtor", soc->flash_base);
    qdev_prop_set_bit(armv7m, "enable-bitband", false);
    qdev_connect_clock_in(armv7m, "cpuclk", s->sysclk);
    qdev_connect_clock_in(armv7m, "refclk", s->refclk);
    object_property_set_link(OBJECT(armv7m), "memory",
                             OBJECT(system_memory), &error_abort);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(armv7m), &error_fatal);

    /* OCTOSPI is initialized after a real QOM Device parent exists so its
     * reusable SSI adapter can attach the independent dm-w25q64 peripheral to
     * an owned bus. */
    {
        const DmMc02BoardFlashProfile *flash = dm_mc02_board_flash(board, 2);
        DmMc02OspiFlashConfig config;

        if (flash) {
            config = (DmMc02OspiFlashConfig) {
                .name = flash->name,
                .storage_size = flash->storage_size,
                .page_size = flash->page_size,
                .sector_size = flash->sector_size,
                .jedec_id = { flash->jedec_id[0], flash->jedec_id[1],
                              flash->jedec_id[2] },
            };
        }
        dm_mc02_ospi_init_with_config_and_ssi(
            &s->ospi2, OBJECT(machine), armv7m, flash ? &config : NULL);
        if (s->ospi2_flash_file && s->ospi2_flash_file[0]) {
            DmNorFlashPersistenceResult result =
                dm_mc02_ospi_load_persistence(&s->ospi2,
                                              s->ospi2_flash_file);

            if (result != DM_NOR_FLASH_PERSISTENCE_OK &&
                result != DM_NOR_FLASH_PERSISTENCE_NOT_FOUND) {
                warn_report("unable to load DM-MC02 OCTOSPI2 Flash from '%s' "
                            "(result=%d); using erased image",
                            s->ospi2_flash_file, result);
            }
        }
        if (flash && flash->memory_mapped) {
            memory_region_add_subregion(system_memory,
                                        soc->ospi2_memory_base,
                                        &s->ospi2.flash_window);
        }
    }
    memory_region_add_subregion(system_memory, soc->ospi2_base,
                                &s->ospi2.iomem);
    {
        const DmMc02BoardFlashProfile *flash = dm_mc02_board_flash(board, 1);
        DmMc02OspiFlashConfig config;

        if (flash) {
            config = (DmMc02OspiFlashConfig) {
                .name = flash->name,
                .storage_size = flash->storage_size,
                .page_size = flash->page_size,
                .sector_size = flash->sector_size,
                .jedec_id = { flash->jedec_id[0], flash->jedec_id[1],
                              flash->jedec_id[2] },
            };
        }
        dm_mc02_ospi_init_with_config_and_ssi(
            &s->ospi1, OBJECT(machine), armv7m, flash ? &config : NULL);
    }
    memory_region_add_subregion(system_memory, soc->ospi1_base,
                                &s->ospi1.iomem);
    dm_mc02_ospim_init(&s->ospim, OBJECT(machine));
    memory_region_add_subregion(system_memory, soc->ospim_base,
                                &s->ospim.iomem);

    /* Board profile owns the peripheral-to-NVIC interrupt wiring. */
    dm_mc02_board_connect_irqs(
        board, OBJECT(machine), armv7m, &s->gpio_exti.exti, &s->adc[0],
        &s->adc[1],
        &s->adc_irq_or, &s->tim2, s->tim_aux, s->fdcan, s->uart, &s->rng,
        &s->dma_subsystem.dma[0], &s->dma_subsystem.dma[1],
        &s->usb_hs, &s->wwdg1, &error_fatal);

    armv7m_load_kernel(ARM_CPU(first_cpu), machine->kernel_filename,
                       soc->flash_base, soc->flash_size);

    /* Preserve the H723 software-reset cause before QEMU processes the
     * reset request.  The ARMv7-M container emits this named output for
     * guest AIRCR.SYSRESETREQ; PWR/RCC owns the latched RCC_RSR state. */
    s->sysresetreq_irq = qemu_allocate_irq(
        dm_mc02_pwr_rcc_note_software_reset, &s->pwr_rcc, 0);
    qdev_connect_gpio_out_named(armv7m, "SYSRESETREQ", 0,
                                s->sysresetreq_irq);
    dm_mc02_apply_mcu_power_state(s);
    dm_mc02_flash_set_program_enabled(&s->flash_regs, false);

    /* serial_hd(0) is optional.  With no -serial chardev, the legacy
     * dm-mc02 machine and its SPI2/BMI088 path are unchanged. */
    dm_mc02_cosim_link_init(&s->cosim, serial_hd(0), dm_mc02_cosim_imu,
                            dm_mc02_cosim_telemetry, s, &error_fatal);
    dm_mc02_cosim_link_set_adc_handler(&s->cosim, dm_mc02_cosim_adc);
    dm_mc02_cosim_link_set_adc_pin_voltage_handler(
        &s->cosim, dm_mc02_cosim_adc_pin_voltage);
    if (s->cosim_motor_loopback) {
        dm_mc02_cosim_link_set_motor_step_handler(
            &s->cosim, dm_mc02_cosim_motor_loopback);
    }

    s->flash_shutdown_notifier.notify = dm_mc02_persist_flash;
    qemu_register_shutdown_notifier(&s->flash_shutdown_notifier);

    /* Firmware writes the WS2812 buffer without touching a QEMU-visible
     * register, so periodically observe it in virtual time. */
    s->ws2812_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                   dm_mc02_ws2812_refresh, s);
    if (s->cosim.enabled) {
        timer_mod(s->ws2812_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10 * 1000 * 1000);
    }
    qemu_register_reset(dm_mc02_machine_reset, s);
}

static void dm_mc02_finalize(Object *obj)
{
    DmMc02MachineState *s = DM_MC02_MACHINE(obj);

    qemu_unregister_reset(dm_mc02_machine_reset, s);
    for (size_t i = 0; i < s->initialized_uart_count; ++i) {
        dm_mc02_uart_cleanup(&s->uart[i]);
    }
    for (size_t i = 0; i < s->initialized_fdcan_count; ++i) {
        dm_mc02_fdcan_cleanup(&s->fdcan[i]);
    }
    dm_mc02_spi_cleanup(&s->bmi_spi_link.spi);
    dm_mc02_spi_cleanup(&s->spi1_state);
    timer_free(s->iwdg1.timeout_timer);
    timer_free(s->iwdg1.update_timer);
    timer_free(s->wwdg1.timer);
    timer_free(s->ws2812_timer);
    timer_free(s->tim2.update_timer);
    timer_free(s->tim2.compare_timer);
    for (size_t i = 0; i < s->initialized_timer_count; ++i) {
        timer_free(s->tim_aux[i].update_timer);
        timer_free(s->tim_aux[i].compare_timer);
    }
    dm_mc02_cosim_link_cleanup(&s->cosim);
    if (s->sysresetreq_irq) {
        qemu_free_irq(s->sysresetreq_irq);
    }
    dm_mc02_usb_cleanup(&s->usb_hs);
    dm_mc02_ospi_cleanup(&s->ospi2);
    dm_mc02_ospi_cleanup(&s->ospi1);
    g_free(s->flash_file);
    g_free(s->ospi2_flash_file);
    g_free(s->gpio_input_spec);
}

static void dm_mc02_machine_class_init(ObjectClass *oc, void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    static const char * const valid_cpu_types[] = {
        ARM_CPU_TYPE_NAME("cortex-m7"),
        NULL
    };

    (void)data;

    mc->desc = "DM-MC02 minimal Cortex-M7 memory-map machine";
    mc->init = dm_mc02_init;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("cortex-m7");
    mc->valid_cpu_types = valid_cpu_types;
    mc->default_ram_size = 0;
    mc->max_cpus = 1;
}

static const TypeInfo dm_mc02_machine_type = {
    .name = TYPE_DM_MC02_MACHINE,
    .parent = TYPE_MACHINE,
    .instance_size = sizeof(DmMc02MachineState),
    .instance_init = dm_mc02_machine_instance_init,
    .class_init = dm_mc02_machine_class_init,
    .instance_finalize = dm_mc02_finalize,
};

static void dm_mc02_machine_register_types(void)
{
    type_register_static(&dm_mc02_machine_type);
}

type_init(dm_mc02_machine_register_types)
