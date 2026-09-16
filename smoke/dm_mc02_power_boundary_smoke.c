/* Host harness for the real QEMU DM-MC02 board power model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_power.h"
#include "qemu/timer.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static uint64_t virtual_now_ns = 1000;
static uint16_t observed_adc4;
static uint16_t observed_adc19;

int64_t qemu_clock_get_ns(QEMUClockType type)
{
    (void)type;
    return (int64_t)virtual_now_ns;
}

void dm_mc02_adc_set_board_source_raw(DmMc02Adc *adc, uint16_t channel,
                                      uint16_t raw)
{
    (void)adc;
    if (channel == 4) {
        observed_adc4 = raw;
    } else if (channel == 19) {
        observed_adc19 = raw;
    }
}

static void fail_case(uint32_t vin, const char *what, uint64_t got,
                      uint64_t expected)
{
    fprintf(stderr, "VIN=%" PRIu32 " mV: %s: got=%" PRIu64
                    " expected=%" PRIu64 "\n",
            vin, what, got, expected);
    exit(EXIT_FAILURE);
}

static void expect_u32(uint32_t vin, const char *what, uint32_t got,
                       uint32_t expected)
{
    if (got != expected) {
        fail_case(vin, what, got, expected);
    }
}

static void expect_bool(uint32_t vin, const char *what, bool got, bool expected)
{
    if (got != expected) {
        fail_case(vin, what, got, expected);
    }
}

static void expect_case(DmMc02Power *power, uint32_t vin, uint32_t state,
                        uint16_t adc4, bool system_good)
{
    virtual_now_ns += 100;
    observed_adc4 = 0;
    observed_adc19 = 0;
    dm_mc02_power_set_vin_mv(power, vin);

    expect_u32(vin, "state", dm_mc02_power_get_state(power), state);
    expect_u32(vin, "VIN", dm_mc02_power_get_vin_mv(power), vin);
    expect_u32(vin, "ADC4 model source", dm_mc02_power_get_adc_source_raw(power, 4), adc4);
    expect_u32(vin, "ADC4 observed source", observed_adc4, adc4);
    expect_u32(vin, "ADC19 model source", dm_mc02_power_get_adc_source_raw(power, 19),
               system_good ? UINT16_MAX : 0);
    expect_u32(vin, "ADC19 observed source", observed_adc19,
               system_good ? UINT16_MAX : 0);
    expect_bool(vin, "system 5V good", dm_mc02_power_get_system_5v_good(power),
                system_good);
    expect_bool(vin, "system 3V3 good", dm_mc02_power_get_system_3v3_good(power),
                system_good);
    expect_u32(vin, "last update timestamp",
               (uint32_t)dm_mc02_power_get_last_update_ns(power),
               (uint32_t)virtual_now_ns);
}

int main(void)
{
    DmMc02Adc adc = { 0 };
    DmMc02Power power;

    dm_mc02_power_init(&power, &adc);
    expect_case(&power, 0, DM_MC02_POWER_OFF, 0, false);
    expect_case(&power, 1, DM_MC02_POWER_UNDERVOLTAGE, 2, false);
    expect_case(&power, 11999, DM_MC02_POWER_UNDERVOLTAGE, 21663, false);
    expect_case(&power, 12000, DM_MC02_POWER_NORMAL, 21664, true);
    expect_case(&power, 24000, DM_MC02_POWER_NORMAL, 43329, true);
    expect_case(&power, 36300, DM_MC02_POWER_NORMAL, UINT16_MAX, true);
    /* 40 V is above the 36.3 V divider range and must saturate at VREF. */
    expect_case(&power, 40000, DM_MC02_POWER_NORMAL, UINT16_MAX, true);

    dm_mc02_power_set_gpio_odr(&power,
        (UINT32_C(1) << DM_MC02_POWER_PC13_24V2_BIT) |
        (UINT32_C(1) << DM_MC02_POWER_PC14_24V1_BIT) |
        (UINT32_C(1) << DM_MC02_POWER_PC15_5V_BIT));
    expect_bool(24000, "24V1 enable", dm_mc02_power_get_out1_enabled(&power), true);
    expect_bool(24000, "24V2 enable", dm_mc02_power_get_out2_enabled(&power), true);
    expect_bool(24000, "5V switch enable", dm_mc02_power_get_5v_enabled(&power), true);
    expect_bool(24000, "24V1 good", dm_mc02_power_get_out1_good(&power), true);
    expect_bool(24000, "24V2 good", dm_mc02_power_get_out2_good(&power), true);

    /* Electrical policy makes the switched 5 V rail an actual boundary;
     * ideal policy above intentionally preserves the historical behavior. */
    dm_mc02_power_set_electrical_policy(&power, true);
    dm_mc02_power_set_gpio_odr(&power, 0);
    expect_bool(24000, "electrical policy with 5V switch off",
                dm_mc02_power_get_system_5v_good(&power), false);
    dm_mc02_power_set_gpio_odr(&power,
        (UINT32_C(1) << DM_MC02_POWER_PC15_5V_BIT));
    expect_bool(24000, "electrical policy with 5V switch on",
                dm_mc02_power_get_system_5v_good(&power), true);

    dm_mc02_power_set_vin_mv(&power, 0);
    expect_bool(0, "24V1 disabled by VIN", dm_mc02_power_get_out1_good(&power), false);
    expect_bool(0, "24V2 disabled by VIN", dm_mc02_power_get_out2_good(&power), false);

    /* VIN is an external supply condition and must survive an MCU reset. */
    dm_mc02_power_set_vin_mv(&power, 18000);
    dm_mc02_power_set_gpio_odr(&power,
        UINT32_C(1) << DM_MC02_POWER_PC15_5V_BIT);
    dm_mc02_power_reset(&power);
    expect_u32(18000, "VIN survives reset", dm_mc02_power_get_vin_mv(&power),
               18000);
    expect_u32(18000, "VIN ADC source survives reset",
               dm_mc02_power_get_adc_source_raw(&power, 4), 32497);
    expect_bool(18000, "external 5V switch resets low",
                dm_mc02_power_get_5v_enabled(&power), false);

    puts("RESULT: DM-MC02 board power VIN boundary smoke passed");
    puts("  checked VIN=0,1,11999,12000,24000,36300,40000 mV");
    puts("  checked power state, system rails, GPIO enables, ADC4 divider and saturation");
    return EXIT_SUCCESS;
}
