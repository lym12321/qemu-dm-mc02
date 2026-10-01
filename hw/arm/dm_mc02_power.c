/*
 * DM-MC02 board power model.
 *
 * This is intentionally a discrete board-source model.  It captures input
 * power, enable pins and the normal/undervoltage/off boundaries without
 * pretending to model converter transients or analog switch behavior.
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "hw/arm/dm_mc02_power.h"

#define DM_MC02_POWER_ADC_PIN_MAX_UV 3300000u

static uint16_t power_vin_to_adc_raw(uint32_t vin_mv)
{
    uint64_t numerator;

    /* PC4 sees VIN through the 11:1 divider; the ADC input saturates at VREF. */
    numerator = (uint64_t)vin_mv * 1000u * UINT16_MAX;
    numerator += 11u * DM_MC02_POWER_ADC_PIN_MAX_UV / 2u;
    numerator /= 11u * DM_MC02_POWER_ADC_PIN_MAX_UV;
    return MIN(numerator, (uint64_t)UINT16_MAX);
}

static DmMc02PowerState power_state_for_vin(uint32_t vin_mv)
{
    if (vin_mv == 0) {
        return DM_MC02_POWER_OFF;
    }
    if (vin_mv < DM_MC02_POWER_UNDERVOLTAGE_MV) {
        return DM_MC02_POWER_UNDERVOLTAGE;
    }
    return DM_MC02_POWER_NORMAL;
}

void dm_mc02_power_update(DmMc02Power *power)
{
    DmMc02PowerState previous_state;
    bool normal;

    if (!power) {
        return;
    }
    previous_state = power->state;
    power->state = power_state_for_vin(power->vin_mv);
    power->out2_enabled = (power->gpio_odr &
                           power->out2_mask) != 0;
    power->out1_enabled = (power->gpio_odr &
                           power->out1_mask) != 0;
    power->switched_5v_enabled = (power->gpio_odr &
                                  power->switched_5v_mask) != 0;

    normal = power->state == DM_MC02_POWER_NORMAL;
    power->system_5v_good = normal &&
                            (!power->electrical_power ||
                             power->switched_5v_enabled);
    power->system_3v3_good = power->system_5v_good;
    power->out1_good = normal && power->out1_enabled;
    power->out2_good = normal && power->out2_enabled;

    /* VIN sense is present even when the two switched outputs are disabled. */
    power->adc4_raw = power_vin_to_adc_raw(power->vin_mv);
    power->adc19_raw = power->system_3v3_good ? UINT16_MAX : 0;
    if (power->adc) {
        dm_mc02_adc_set_board_source_raw(power->adc,
                                         power->vin_adc_channel,
                                         power->adc4_raw);
        dm_mc02_adc_set_board_source_raw(power->adc,
                                         power->key_adc_channel,
                                         power->adc19_raw);
    }
    power->last_update_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    /* Only the descending crossing is a brownout event.  In particular, do
     * not turn initial low VIN, VIN=0 power removal, or a recovery edge into
     * a BOR source. */
    if (previous_state == DM_MC02_POWER_NORMAL &&
        power->state == DM_MC02_POWER_UNDERVOLTAGE &&
        power->brownout_callback) {
        power->brownout_callback(power->brownout_opaque);
    }
}

void dm_mc02_power_init(DmMc02Power *power, DmMc02Adc *adc)
{
    if (!power) {
        return;
    }
    memset(power, 0, sizeof(*power));
    power->adc = adc;
    power->vin_mv = DM_MC02_POWER_DEFAULT_VIN_MV;
    power->out1_mask = UINT32_C(1) << DM_MC02_POWER_PC14_24V1_BIT;
    power->out2_mask = UINT32_C(1) << DM_MC02_POWER_PC13_24V2_BIT;
    power->switched_5v_mask = UINT32_C(1) << DM_MC02_POWER_PC15_5V_BIT;
    power->vin_adc_channel = 4;
    power->key_adc_channel = 19;
    dm_mc02_power_update(power);
}

void dm_mc02_power_set_electrical_policy(DmMc02Power *power, bool enabled)
{
    if (power) {
        power->electrical_power = enabled;
        dm_mc02_power_update(power);
    }
}

void dm_mc02_power_reset(DmMc02Power *power)
{
    bool electrical;
    DmMc02Adc *adc;
    uint32_t vin_mv;
    uint32_t out1_mask;
    uint32_t out2_mask;
    uint32_t switched_5v_mask;
    uint16_t vin_adc_channel;
    uint16_t key_adc_channel;
    DmMc02PowerBrownoutCallback *brownout_callback;
    void *brownout_opaque;

    if (!power) {
        return;
    }
    electrical = power->electrical_power;
    adc = power->adc;
    /* VIN is an external board condition, not an MCU peripheral register;
     * preserve it across warm/cold MCU reset just as a bench supply would. */
    vin_mv = power->vin_mv;
    out1_mask = power->out1_mask;
    out2_mask = power->out2_mask;
    switched_5v_mask = power->switched_5v_mask;
    vin_adc_channel = power->vin_adc_channel;
    key_adc_channel = power->key_adc_channel;
    brownout_callback = power->brownout_callback;
    brownout_opaque = power->brownout_opaque;
    memset(power, 0, sizeof(*power));
    power->adc = adc;
    power->electrical_power = electrical;
    power->vin_mv = vin_mv;
    power->out1_mask = out1_mask;
    power->out2_mask = out2_mask;
    power->switched_5v_mask = switched_5v_mask;
    power->vin_adc_channel = vin_adc_channel;
    power->key_adc_channel = key_adc_channel;
    power->brownout_callback = brownout_callback;
    power->brownout_opaque = brownout_opaque;
    dm_mc02_power_update(power);
}

void dm_mc02_power_set_vin_mv(DmMc02Power *power, uint32_t vin_mv)
{
    if (!power) {
        return;
    }
    power->vin_mv = vin_mv;
    dm_mc02_power_update(power);
}

void dm_mc02_power_set_gpio_odr(DmMc02Power *power, uint32_t odr)
{
    if (!power) {
        return;
    }
    power->gpio_odr = odr & 0xffffu;
    dm_mc02_power_update(power);
}

void dm_mc02_power_set_wiring(DmMc02Power *power, uint32_t out1_mask,
                              uint32_t out2_mask, uint32_t switched_5v_mask,
                              uint16_t vin_adc_channel,
                              uint16_t key_adc_channel)
{
    if (!power) {
        return;
    }
    power->out1_mask = out1_mask;
    power->out2_mask = out2_mask;
    power->switched_5v_mask = switched_5v_mask;
    power->vin_adc_channel = vin_adc_channel;
    power->key_adc_channel = key_adc_channel;
    dm_mc02_power_update(power);
}

void dm_mc02_power_set_brownout_callback(
    DmMc02Power *power, DmMc02PowerBrownoutCallback *callback, void *opaque)
{
    if (!power) {
        return;
    }
    power->brownout_callback = callback;
    power->brownout_opaque = opaque;
}

bool dm_mc02_power_state_valid(const DmMc02Power *power)
{
    /* gpio_odr is sampled from a 16-bit GPIO bank.  The normal setter masks
     * it, so a VMState load must preserve the same producer contract. */
    return power && power->gpio_odr <= 0xffffu;
}

void dm_mc02_power_sync_runtime(DmMc02Power *power)
{
    dm_mc02_power_update(power);
}

DmMc02PowerState dm_mc02_power_get_state(const DmMc02Power *power)
{
    return power ? power->state : DM_MC02_POWER_OFF;
}

uint32_t dm_mc02_power_get_vin_mv(const DmMc02Power *power)
{
    return power ? power->vin_mv : 0;
}

uint32_t dm_mc02_power_get_gpio_odr(const DmMc02Power *power)
{
    return power ? power->gpio_odr : 0;
}

bool dm_mc02_power_get_out1_enabled(const DmMc02Power *power)
{
    return power && power->out1_enabled;
}

bool dm_mc02_power_get_out2_enabled(const DmMc02Power *power)
{
    return power && power->out2_enabled;
}

bool dm_mc02_power_get_5v_enabled(const DmMc02Power *power)
{
    return power && power->switched_5v_enabled;
}

bool dm_mc02_power_get_system_5v_good(const DmMc02Power *power)
{
    return power && power->system_5v_good;
}

bool dm_mc02_power_get_system_3v3_good(const DmMc02Power *power)
{
    return power && power->system_3v3_good;
}

bool dm_mc02_power_get_out1_good(const DmMc02Power *power)
{
    return power && power->out1_good;
}

bool dm_mc02_power_get_out2_good(const DmMc02Power *power)
{
    return power && power->out2_good;
}

uint16_t dm_mc02_power_get_adc_source_raw(const DmMc02Power *power,
                                          uint16_t channel)
{
    if (!power) {
        return 0;
    }
    if (channel == power->vin_adc_channel) {
        return power->adc4_raw;
    }
    if (channel == power->key_adc_channel) {
        return power->adc19_raw;
    }
    return 0;
}

uint64_t dm_mc02_power_get_last_update_ns(const DmMc02Power *power)
{
    return power ? power->last_update_ns : 0;
}
