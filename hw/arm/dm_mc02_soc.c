/* Canonical STM32H723 memory map used by the reusable chip layer. */
#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "hw/arm/dm_mc02_soc.h"

#define DM_MC02_SOC_ADC_REGION_SIZE 0x100

static bool dm_mc02_soc_ranges_overlap(hwaddr first, hwaddr first_size,
                                       hwaddr second, hwaddr second_size)
{
    return first < second + second_size && second < first + first_size;
}

const DmMc02SocProfile dm_mc02_stm32h723_soc = {
    .name = "STM32H723",
    .cpu_type = DM_MC02_ARMV7M_CORTEX_M7_CPU_TYPE,
    .irq_count = 163,
    .reset_cpu_hz = 64000000ULL,
    .refclk_hz = 1000000ULL,
    .flash_base = 0x08000000,
    .flash_size = 1024 * 1024,
    .itcm_base = 0x00000000,
    .itcm_size = 64 * 1024,
    .dtcm_base = 0x20000000,
    .dtcm_size = 128 * 1024,
    .axi_sram_base = 0x24000000,
    .axi_sram_size = 320 * 1024,
    .d2_sram_base = 0x30000000,
    .d2_sram_size = 32 * 1024,
    .d3_sram_base = 0x38000000,
    .d3_sram_size = 16 * 1024,
    .calibration_base = 0x1FF1E000,
    .calibration_size = 0x1000,
    .spi1_base = 0x40013000,
    .spi2_base = 0x40003800,
    .gpio_base = 0x58020000,
    .gpio_bank_count = 8,
    .rcc_base = 0x58024400,
    .pwr_base = 0x58024800,
    .flash_r_base = 0x52002000,
    .fmc_r_base = 0x52004000,
    .syscfg_base = 0x58000400,
    .exti_base = 0x58000000,
    .dma1_base = 0x40020000,
    .dma2_base = 0x40020400,
    .dmamux1_base = 0x40020800,
    .dmamux2_base = 0x40020c00,
    .fdcan_msg_ram_base = 0x4000ac00,
    .fdcan_msg_ram_size = 0x2800,
    .tim2_base = 0x40000000,
    .adc1_base = 0x40022000,
    .adc2_base = 0x40022100,
    .adc_common_base = 0x40022300,
    .dbgmcu_base = 0x5C001000,
    .rng_base = 0x48021800,
    .crc_base = 0x58024C00,
    .ospi1_base = 0x52005000,
    .ospi2_base = 0x5200A000,
    .ospim_base = 0x5200B400,
    .ospi2_memory_base = 0x70000000,
    .iwdg1_base = 0x58004800,
    .wwdg1_base = 0x50003000,
    .usb_hs_base = 0x40040000,
    .cordic_base = 0x58004400,
};

const DmMc02SocProfile *dm_mc02_soc_stm32h723(void)
{
    return &dm_mc02_stm32h723_soc;
}

bool dm_mc02_soc_validate_map(const DmMc02SocProfile *soc)
{
    if (!soc || !soc->tim2_base || !soc->adc1_base || !soc->adc2_base ||
        !soc->adc_common_base) {
        return false;
    }

    return !dm_mc02_soc_ranges_overlap(soc->adc1_base,
                                       DM_MC02_SOC_ADC_REGION_SIZE,
                                       soc->adc2_base,
                                       DM_MC02_SOC_ADC_REGION_SIZE) &&
           !dm_mc02_soc_ranges_overlap(soc->adc1_base,
                                       DM_MC02_SOC_ADC_REGION_SIZE,
                                       soc->adc_common_base,
                                       DM_MC02_SOC_ADC_REGION_SIZE) &&
           !dm_mc02_soc_ranges_overlap(soc->adc2_base,
                                       DM_MC02_SOC_ADC_REGION_SIZE,
                                       soc->adc_common_base,
                                       DM_MC02_SOC_ADC_REGION_SIZE);
}

void dm_mc02_soc_memory_init(DmMc02SocMemory *memory, Object *owner,
                             MemoryRegion *system_memory,
                             const DmMc02SocProfile *soc, Error **errp)
{
    uint8_t *calib;

    (void)owner;

    /* The machine owns the backing bytes, but MachineState is not a
     * DeviceState.  A NULL owner gives QEMU a global RAMBlock id and keeps
     * the region migratable without making the SoC depend on machine QOM
     * details. */
    memory_region_init_ram(&memory->flash, NULL, "dm-mc02.flash",
                           soc->flash_size, errp);
    memset(memory_region_get_ram_ptr(&memory->flash), 0xff, soc->flash_size);
    memory_region_add_subregion(system_memory, soc->flash_base,
                                &memory->flash);

    memory_region_init_ram(&memory->itcm, NULL, "dm-mc02.itcm",
                           soc->itcm_size, errp);
    memory_region_add_subregion(system_memory, soc->itcm_base,
                                &memory->itcm);
    memory_region_init_ram(&memory->dtcm, NULL, "dm-mc02.dtcm",
                           soc->dtcm_size, errp);
    memory_region_add_subregion(system_memory, soc->dtcm_base,
                                &memory->dtcm);
    memory_region_init_ram(&memory->axi_sram, NULL, "dm-mc02.axi-sram",
                           soc->axi_sram_size, errp);
    memory_region_add_subregion(system_memory, soc->axi_sram_base,
                                &memory->axi_sram);
    memory_region_init_ram(&memory->d2_sram, NULL, "dm-mc02.d2-sram",
                           soc->d2_sram_size, errp);
    memory_region_add_subregion(system_memory, soc->d2_sram_base,
                                &memory->d2_sram);
    memory_region_init_ram(&memory->d3_sram, NULL, "dm-mc02.d3-sram",
                           soc->d3_sram_size, errp);
    memory_region_add_subregion(system_memory, soc->d3_sram_base,
                                &memory->d3_sram);

    /* FDCAN1/2/3 share one SoC-owned message RAM.  Use QEMU's normal RAM
     * owner so the bytes have a migration owner before machine composition
     * defines its device restore order. */
    if (!dm_message_ram_init(&memory->fdcan_msg_ram, NULL,
                             "dm-mc02.fdcan-msg-ram",
                             soc->fdcan_msg_ram_size, errp)) {
        return;
    }
    dm_message_ram_reset(&memory->fdcan_msg_ram);
    memory_region_add_subregion(system_memory, soc->fdcan_msg_ram_base,
                                dm_message_ram_region(&memory->fdcan_msg_ram));

    /* STM32H723 UID and ADC factory calibration are immutable SoC data. */
    /* Factory calibration/UID bytes are immutable profile data rebuilt at
     * realize time.  They are intentionally not part of the RAM stream. */
    memory_region_init_rom_nomigrate(&memory->calibration_rom, NULL,
                                     "dm-mc02.calibration-rom",
                                     soc->calibration_size, errp);
    calib = memory_region_get_ram_ptr(&memory->calibration_rom);
    memset(calib, 0, soc->calibration_size);
    stl_le_p(calib + 0x800, 0x12345678);
    stl_le_p(calib + 0x804, 0x9abcdef0);
    stl_le_p(calib + 0x808, 0x13579bdf);
    stw_le_p(calib + 0x820, 1000);
    stw_le_p(calib + 0x840, 1500);
    stw_le_p(calib + 0x860, 1500);
    memory_region_add_subregion(system_memory, soc->calibration_base,
                                &memory->calibration_rom);
}

void dm_mc02_soc_memory_reset_volatile(DmMc02SocMemory *memory,
                                       const DmMc02SocProfile *soc)
{
    memset(memory_region_get_ram_ptr(&memory->itcm), 0, soc->itcm_size);
    memset(memory_region_get_ram_ptr(&memory->dtcm), 0, soc->dtcm_size);
    memset(memory_region_get_ram_ptr(&memory->axi_sram), 0, soc->axi_sram_size);
    memset(memory_region_get_ram_ptr(&memory->d2_sram), 0, soc->d2_sram_size);
    memset(memory_region_get_ram_ptr(&memory->d3_sram), 0, soc->d3_sram_size);
}

void dm_mc02_soc_memory_reset_fdcan_msg_ram(DmMc02SocMemory *memory)
{
    dm_message_ram_reset(memory ? &memory->fdcan_msg_ram : NULL);
}
