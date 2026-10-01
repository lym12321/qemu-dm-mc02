/* Runtime-sync stub for the isolated ADC VMState contract test. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc.h"

unsigned dm_mc02_adc_sync_calls;

void dm_mc02_adc_sync_runtime(DmMc02Adc *state)
{
    (void)state;
    dm_mc02_adc_sync_calls++;
}
