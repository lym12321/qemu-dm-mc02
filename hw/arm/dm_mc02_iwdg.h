/* Minimal STM32H723 independent watchdog register window. */
#ifndef HW_ARM_DM_MC02_IWDG_H
#define HW_ARM_DM_MC02_IWDG_H

#include "exec/memory.h"
#include "hw/arm/dm_mc02_iwdg_timing.h"
#include "migration/vmstate.h"
#include "qemu/timer.h"

#include <stdint.h>

#define DM_MC02_IWDG_REGION_SIZE 0x100

#define DM_MC02_IWDG_KR_OFFSET   0x00
#define DM_MC02_IWDG_PR_OFFSET   0x04
#define DM_MC02_IWDG_RLR_OFFSET  0x08
#define DM_MC02_IWDG_SR_OFFSET   0x0c
#define DM_MC02_IWDG_WINR_OFFSET 0x10
#define DM_MC02_IWDG_DEFAULT_LSI_HZ UINT32_C(32000)
#define DM_MC02_IWDG_SR_PVU (1u << 0)
#define DM_MC02_IWDG_SR_RVU (1u << 1)
#define DM_MC02_IWDG_SR_WVU (1u << 2)
#define DM_MC02_IWDG_SR_UPDATE_MASK \
    (DM_MC02_IWDG_SR_PVU | DM_MC02_IWDG_SR_RVU | DM_MC02_IWDG_SR_WVU)
#define DM_MC02_IWDG_UPDATE_REGISTER_COUNT 3

typedef void DmMc02IwdgResetRequested(void *opaque);

typedef struct DmMc02Iwdg {
    MemoryRegion iomem;
    QEMUTimer *timeout_timer;
    QEMUTimer *update_timer;
    uint32_t regs[DM_MC02_IWDG_REGION_SIZE / sizeof(uint32_t)];
    /* Configuration writes cross into the LSI domain after the deterministic
     * status-update delay.  regs[] always contains the guest-visible,
     * committed values while these fields are the not-yet-visible producer
     * state.  Each register has its own deadline and SR update bit. */
    uint32_t pending_pr;
    uint32_t pending_rlr;
    uint32_t pending_winr;
    uint64_t update_deadline_ns[DM_MC02_IWDG_UPDATE_REGISTER_COUNT];
    /* LSI settings are machine/component configuration, not guest state. */
    uint32_t lsi_hz;
    int32_t lsi_error_ppm;
    uint64_t boot_grace_ns;
    bool boot_grace_pending;
    bool write_unlocked;
    bool started;
    /* Absolute virtual deadline for the active watchdog window.  The timer
     * itself is runtime wiring; this value is the producer state needed to
     * rebuild it after a component restore. */
    uint64_t next_timeout_ns;
    /* Monotonic diagnostics survive a guest reset. */
    uint64_t start_count;
    uint64_t reload_count;
    uint64_t timeout_count;
    uint64_t window_violation_count;
    /* Destination-owned board hook.  The IWDG core does not know which
     * reset-status block, if any, consumes this event. */
    DmMc02IwdgResetRequested *reset_requested;
    void *reset_requested_opaque;
} DmMc02Iwdg;

void dm_mc02_iwdg_init(DmMc02Iwdg *state, Object *owner);
void dm_mc02_iwdg_reset(void *opaque);
/* LSI frequency is a positive startup/next-arm configuration.  A zero value
 * is invalid and is never silently replaced with the default. */
bool dm_mc02_iwdg_set_lsi_hz(DmMc02Iwdg *state, uint32_t lsi_hz);
bool dm_mc02_iwdg_set_lsi_error_ppm(DmMc02Iwdg *state, int32_t error_ppm);
void dm_mc02_iwdg_set_boot_grace_ms(DmMc02Iwdg *state, uint32_t grace_ms);
void dm_mc02_iwdg_set_reset_callback(DmMc02Iwdg *state,
                                     DmMc02IwdgResetRequested *callback,
                                     void *opaque);

/* Rebuild the runtime timer from the restored virtual deadline.  The nominal
 * LSI, fixed error and boot-grace values remain caller-owned configuration and
 * are not serialized. */
void dm_mc02_iwdg_sync_runtime(DmMc02Iwdg *state);

/* Component-only state contract; the DM-MC02 machine does not register it. */
const VMStateDescription *dm_mc02_iwdg_vmstate(void);

#endif
