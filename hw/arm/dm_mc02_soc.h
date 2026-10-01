/* Reusable STM32H723-class memory and core description. */
#ifndef HW_ARM_DM_MC02_SOC_H
#define HW_ARM_DM_MC02_SOC_H

#include "exec/hwaddr.h"
#include "exec/memory.h"
#include "hw/misc/dm_message_ram.h"
#include "qapi/error.h"
#include "qom/object.h"

#include <stdbool.h>
#include <stdint.h>

/* ARMv7M's cpu-type property takes the complete QOM type name. */
#define DM_MC02_ARMV7M_CORTEX_M7_CPU_TYPE "cortex-m7-arm-cpu"

typedef struct DmMc02SocProfile {
    const char *name;
    const char *cpu_type;
    unsigned irq_count;
    uint64_t reset_cpu_hz;
    uint64_t refclk_hz;

    hwaddr flash_base;
    uint32_t flash_size;
    hwaddr itcm_base;
    uint32_t itcm_size;
    hwaddr dtcm_base;
    uint32_t dtcm_size;
    hwaddr axi_sram_base;
    uint32_t axi_sram_size;
    hwaddr d2_sram_base;
    uint32_t d2_sram_size;
    hwaddr d3_sram_base;
    uint32_t d3_sram_size;
    hwaddr calibration_base;
    uint32_t calibration_size;

    hwaddr spi1_base;
    hwaddr spi2_base;
    hwaddr gpio_base;
    unsigned gpio_bank_count;
    hwaddr rcc_base;
    hwaddr pwr_base;
    hwaddr flash_r_base;
    hwaddr fmc_r_base;
    hwaddr syscfg_base;
    hwaddr exti_base;
    hwaddr dma1_base;
    hwaddr dma2_base;
    hwaddr dmamux1_base;
    hwaddr dmamux2_base;
    hwaddr fdcan_msg_ram_base;
    uint32_t fdcan_msg_ram_size;
    hwaddr adc1_base;
    hwaddr adc2_base;
    hwaddr adc_common_base;
    hwaddr dbgmcu_base;
    hwaddr rng_base;
    hwaddr crc_base;
    hwaddr ospi1_base;
    hwaddr ospi2_base;
    hwaddr ospim_base;
    hwaddr ospi2_memory_base;
    hwaddr iwdg1_base;
    hwaddr wwdg1_base;
    hwaddr usb_hs_base;
    hwaddr cordic_base;
    /* Appended to preserve the order of the pre-existing profile fields. */
    hwaddr tim2_base;
} DmMc02SocProfile;

/* Memory owned by the H723-class SoC.  Board models may add their own
 * external memories, but these regions and their aliases belong here. */
typedef struct DmMc02SocMemory {
    MemoryRegion flash;
    MemoryRegion itcm;
    MemoryRegion dtcm;
    MemoryRegion axi_sram;
    MemoryRegion d2_sram;
    MemoryRegion d3_sram;
    MemoryRegion calibration_rom;
    DmMessageRam fdcan_msg_ram;
} DmMc02SocMemory;

extern const DmMc02SocProfile dm_mc02_stm32h723_soc;

const DmMc02SocProfile *dm_mc02_soc_stm32h723(void);

/* Validate the fixed-address portion of an SoC profile without side effects. */
bool dm_mc02_soc_validate_map(const DmMc02SocProfile *soc);

void dm_mc02_soc_memory_init(DmMc02SocMemory *memory, Object *owner,
                             MemoryRegion *system_memory,
                             const DmMc02SocProfile *soc, Error **errp);
void dm_mc02_soc_memory_reset_volatile(DmMc02SocMemory *memory,
                                       const DmMc02SocProfile *soc);
void dm_mc02_soc_memory_reset_fdcan_msg_ram(DmMc02SocMemory *memory);

#endif
