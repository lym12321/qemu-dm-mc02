/*
 * Minimal STM32H723 power and reset/clock control model for DM-MC02.
 *
 * The model deliberately exposes the complete 0x400-byte register windows.
 * Implemented status bits are synthesized where the firmware polls them;
 * otherwise registers are harmless read/write storage.  The small clock-tree
 * subset needed by the board firmware drives the ARMv7M cpu clock dynamically.
 */
#ifndef HW_ARM_DM_MC02_PWR_RCC_H
#define HW_ARM_DM_MC02_PWR_RCC_H

#include "exec/memory.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_PWR_RCC_REGION_SIZE 0x400
#define DM_MC02_RCC_RSR_OFFSET       0xd0
#define DM_MC02_RCC_RSR_RMVF        (1u << 16)
#define DM_MC02_RCC_RSR_PINRSTF     (1u << 22)
#define DM_MC02_RCC_RSR_BORRSTF     (1u << 21)
#define DM_MC02_RCC_RSR_PORRSTF     (1u << 23)
#define DM_MC02_RCC_RSR_SFTRSTF     (1u << 24)
#define DM_MC02_RCC_RSR_IWDG1RSTF   (1u << 26)
#define DM_MC02_RCC_RSR_WWDG1RSTF   (1u << 28)
#define DM_MC02_RCC_RSR_RESET_FLAGS \
    (DM_MC02_RCC_RSR_PINRSTF | DM_MC02_RCC_RSR_BORRSTF | \
     DM_MC02_RCC_RSR_PORRSTF | \
     DM_MC02_RCC_RSR_SFTRSTF | DM_MC02_RCC_RSR_IWDG1RSTF | \
     DM_MC02_RCC_RSR_WWDG1RSTF)

typedef struct DmMc02PwrRcc DmMc02PwrRcc;
typedef void DmMc02PwrRccClockChanged(void *opaque, uint64_t hz);

typedef struct DmMc02PwrRccRegBlock {
    DmMc02PwrRcc *state;
    bool pwr;
} DmMc02PwrRccRegBlock;

struct DmMc02PwrRcc {
    MemoryRegion pwr;
    MemoryRegion rcc;
    DmMc02PwrRccRegBlock pwr_block;
    DmMc02PwrRccRegBlock rcc_block;
    uint8_t pwr_regs[DM_MC02_PWR_RCC_REGION_SIZE];
    uint8_t rcc_regs[DM_MC02_PWR_RCC_REGION_SIZE];
    /* RCC_CFGR.SW is the guest request.  Keep the effective source
     * separately so an unready request cannot retime consumers. */
    uint8_t system_clock_source;
    bool adc_clock_configured;
    DmMc02PwrRccClockChanged *clock_changed;
    void *clock_changed_opaque;
};

void dm_mc02_pwr_rcc_init(DmMc02PwrRcc *state, Object *owner);
void dm_mc02_pwr_rcc_reset(DmMc02PwrRcc *state);
void dm_mc02_pwr_rcc_set_clock_callback(DmMc02PwrRcc *state,
                                        DmMc02PwrRccClockChanged *callback,
                                        void *opaque);
/* Reproject the restored effective clock state to destination consumers. */
void dm_mc02_pwr_rcc_sync_runtime(DmMc02PwrRcc *state);

/* Record a reset source before the reset sequence runs.  This is a narrow
 * producer/boundary hook; the IWDG core must not depend on this type. */
void dm_mc02_pwr_rcc_note_iwdg_reset(void *opaque);

/* Record a WWDG1 reset before QEMU consumes the normal reset request. */
void dm_mc02_pwr_rcc_note_wwdg_reset(void *opaque);

/* Record the explicit initial board power-on event.  This is intentionally
 * separate from the ordinary reset callback: a QMP/system reset is not a
 * new power-on event. */
void dm_mc02_pwr_rcc_note_power_on_reset(DmMc02PwrRcc *state);

/* Record the board power model's discrete brownout reset event. */
void dm_mc02_pwr_rcc_note_brownout_reset(DmMc02PwrRcc *state);

/* Record an externally asserted active-low NRST event. */
void dm_mc02_pwr_rcc_note_pin_reset(void *opaque);

/* qemu_irq handler for the ARMv7-M SYSRESETREQ output.  The level is a
 * pulse; only its asserted edge records the software reset source. */
void dm_mc02_pwr_rcc_note_software_reset(void *opaque, int line, int level);

/* Validate serialized producer state without invoking the clock callback. */
bool dm_mc02_pwr_rcc_state_valid(const DmMc02PwrRcc *state);
const VMStateDescription *dm_mc02_pwr_rcc_vmstate(void);
const VMStateDescription *dm_mc02_pwr_rcc_vmstate_raw(void);

/* Child description used by an enclosing clock/board composition. */
extern const VMStateDescription vmstate_dm_mc02_pwr_rcc_raw;

uint64_t dm_mc02_pwr_rcc_cpu_clock_hz(const DmMc02PwrRcc *state);
/* Effective AHB clock from SYSCLK and RCC_D1CFGR.HPRE.  D1CPRE is a
 * separate CPU-only divider and is intentionally not applied here. */
uint64_t dm_mc02_pwr_rcc_hclk_hz(const DmMc02PwrRcc *state);
uint64_t dm_mc02_pwr_rcc_apb1_clock_hz(const DmMc02PwrRcc *state);
uint64_t dm_mc02_pwr_rcc_apb2_clock_hz(const DmMc02PwrRcc *state);
uint64_t dm_mc02_pwr_rcc_apb1_timer_clock_hz(const DmMc02PwrRcc *state);
uint64_t dm_mc02_pwr_rcc_apb2_timer_clock_hz(const DmMc02PwrRcc *state);

/* H723 D2CCIP1R.FDCANSEL-selected kernel clock.  The source has no
 * additional divider: 00=HSE, 01=PLL1Q, 10=PLL2Q, 11=unavailable. */
uint64_t dm_mc02_pwr_rcc_fdcan_kernel_clock_hz(const DmMc02PwrRcc *state);

/* H723 D2CCIP2R-selected USART kernel clocks.  USART1/6/10 use the
 * USART16 group; USART2/3/UART4/5/7/8 use USART234578. */
uint64_t dm_mc02_pwr_rcc_usart16_kernel_clock_hz(
    const DmMc02PwrRcc *state);
uint64_t dm_mc02_pwr_rcc_usart234578_kernel_clock_hz(
    const DmMc02PwrRcc *state);

/* Effective DM-MC02 ADC clock: PLL2P followed by the board's /64 ADC
 * prescaler.  Before a guest configures PLL2, retain the compatibility
 * default so standalone peripheral smoke guests remain usable. */
uint64_t dm_mc02_pwr_rcc_adc_clock_hz(const DmMc02PwrRcc *state);
uint64_t dm_mc02_pwr_rcc_adc_kernel_clock_hz(const DmMc02PwrRcc *state);

#endif
