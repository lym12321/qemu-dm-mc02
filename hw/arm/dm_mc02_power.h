/* Deterministic board-level power and analog-source model for DM-MC02. */
#ifndef HW_ARM_DM_MC02_POWER_H
#define HW_ARM_DM_MC02_POWER_H

#include "hw/arm/dm_mc02_adc.h"
#include "migration/vmstate.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_POWER_DEFAULT_VIN_MV 24000u
#define DM_MC02_POWER_UNDERVOLTAGE_MV 12000u

/* Standalone-model defaults; a board profile may override these values. */
#define DM_MC02_POWER_PC13_24V2_BIT 13u
#define DM_MC02_POWER_PC14_24V1_BIT 14u
#define DM_MC02_POWER_PC15_5V_BIT   15u

typedef enum DmMc02PowerState {
    DM_MC02_POWER_OFF = 0,
    DM_MC02_POWER_UNDERVOLTAGE,
    DM_MC02_POWER_NORMAL,
} DmMc02PowerState;

/* Board-independent notification for the discrete MCU brownout boundary.
 * This is emitted only when a previously normal input crosses below the
 * board model's undervoltage threshold. */
typedef void DmMc02PowerBrownoutCallback(void *opaque);

typedef struct DmMc02Power {
    DmMc02Adc *adc;
    uint32_t vin_mv;
    uint32_t gpio_odr;
    uint32_t out1_mask;
    uint32_t out2_mask;
    uint32_t switched_5v_mask;
    uint16_t vin_adc_channel;
    uint16_t key_adc_channel;
    DmMc02PowerState state;
    bool out1_enabled;
    bool out2_enabled;
    bool switched_5v_enabled;
    bool system_5v_good;
    bool system_3v3_good;
    bool out1_good;
    bool out2_good;
    bool electrical_power;
    uint16_t adc4_raw;
    uint16_t adc19_raw;
    uint64_t last_update_ns;
    DmMc02PowerBrownoutCallback *brownout_callback;
    void *brownout_opaque;
} DmMc02Power;

void dm_mc02_power_init(DmMc02Power *power, DmMc02Adc *adc);

/* Set the external board input in millivolts.  0 is a disconnected input;
 * 1..11999 mV is deterministic undervoltage; >=12000 mV is normal.  The
 * runtime NORMAL -> UNDERVOLTAGE crossing is the only brownout event exposed
 * by this discrete model. */
void dm_mc02_power_set_vin_mv(DmMc02Power *power, uint32_t vin_mv);

/* Consume GPIOC's ODR observation.  This records the requested enables but
 * never writes GPIO state back into the MCU model. */
void dm_mc02_power_set_gpio_odr(DmMc02Power *power, uint32_t odr);
void dm_mc02_power_set_wiring(DmMc02Power *power, uint32_t out1_mask,
                              uint32_t out2_mask, uint32_t switched_5v_mask,
                              uint16_t vin_adc_channel,
                              uint16_t key_adc_channel);
/* Install a runtime consumer for the NORMAL -> UNDERVOLTAGE crossing.  The
 * callback is wiring, not component state, and is preserved over reset. */
void dm_mc02_power_set_brownout_callback(
    DmMc02Power *power, DmMc02PowerBrownoutCallback *callback, void *opaque);
void dm_mc02_power_update(DmMc02Power *power);
void dm_mc02_power_set_electrical_policy(DmMc02Power *power, bool enabled);
void dm_mc02_power_reset(DmMc02Power *power);

/* Component state boundary.  The ADC pointer and profile wiring remain
 * destination-owned runtime state; these helpers restore only the dynamic
 * board inputs and re-project the derived rails/sources. */
bool dm_mc02_power_state_valid(const DmMc02Power *power);
void dm_mc02_power_sync_runtime(DmMc02Power *power);
const VMStateDescription *dm_mc02_power_vmstate(void);
const VMStateDescription *dm_mc02_power_vmstate_raw(void);

DmMc02PowerState dm_mc02_power_get_state(const DmMc02Power *power);
uint32_t dm_mc02_power_get_vin_mv(const DmMc02Power *power);
uint32_t dm_mc02_power_get_gpio_odr(const DmMc02Power *power);
bool dm_mc02_power_get_out1_enabled(const DmMc02Power *power);
bool dm_mc02_power_get_out2_enabled(const DmMc02Power *power);
bool dm_mc02_power_get_5v_enabled(const DmMc02Power *power);
bool dm_mc02_power_get_system_5v_good(const DmMc02Power *power);
bool dm_mc02_power_get_system_3v3_good(const DmMc02Power *power);
bool dm_mc02_power_get_out1_good(const DmMc02Power *power);
bool dm_mc02_power_get_out2_good(const DmMc02Power *power);
uint16_t dm_mc02_power_get_adc_source_raw(const DmMc02Power *power,
                                          uint16_t channel);
uint64_t dm_mc02_power_get_last_update_ns(const DmMc02Power *power);

#endif
