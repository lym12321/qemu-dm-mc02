/* External DM-MC02 reset-pin input. */
#ifndef HW_ARM_DM_MC02_RESET_INPUT_H
#define HW_ARM_DM_MC02_RESET_INPUT_H

#include "hw/sysbus.h"

#define TYPE_DM_MC02_RESET_INPUT "dm-mc02-reset-input"
OBJECT_DECLARE_SIMPLE_TYPE(DmMc02ResetInput, DM_MC02_RESET_INPUT)

typedef void DmMc02ResetInputAsserted(void *opaque);

struct DmMc02ResetInput {
    SysBusDevice parent_obj;

    bool nrst_high;
    DmMc02ResetInputAsserted *asserted;
    void *asserted_opaque;
};

/* The input is an active-low external NRST pin. */
void dm_mc02_reset_input_set_callback(DmMc02ResetInput *state,
                                       DmMc02ResetInputAsserted *callback,
                                       void *opaque);
void dm_mc02_reset_input_reset(DmMc02ResetInput *state);

#endif
