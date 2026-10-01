/* Reusable STM32H723 ADC1/ADC2 + ADC12_COMMON state boundary. */
#ifndef HW_ARM_DM_MC02_ADC_PAIR_H
#define HW_ARM_DM_MC02_ADC_PAIR_H

#include "hw/arm/dm_mc02_adc.h"
#include "hw/arm/dm_mc02_adc_common.h"

#define DM_MC02_ADC_PAIR_COUNT 2

typedef struct DmMc02AdcPair {
    /* The common producer is serialized before its ADC consumers. */
    DmMc02AdcCommon common;
    DmMc02Adc adc[DM_MC02_ADC_PAIR_COUNT];
} DmMc02AdcPair;

/* The callback graph, clocks, QEMUTimers, DMA and IRQs are destination-owned
 * wiring.  This helper validates the cross-component identity invariants
 * without touching that runtime state. */
bool dm_mc02_adc_pair_state_valid(const DmMc02AdcPair *state);

/* Restore order is common clock projection followed by both ADC scheduler
 * projections.  The parent owns this ordering so a child cannot rearm a
 * timer against stale common-clock configuration. */
void dm_mc02_adc_pair_sync_runtime(DmMc02AdcPair *state);

/* Component-only state contract; the DM-MC02 machine does not register it as
 * machine-level migration until CPU, RAM, DMA, IRQ and bus owners are ready. */
const VMStateDescription *dm_mc02_adc_pair_vmstate(void);

#endif
