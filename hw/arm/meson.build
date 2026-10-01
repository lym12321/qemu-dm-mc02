arm_ss = ss.source_set()
arm_ss.add(files('boot.c'), fdt)
arm_ss.add(when: 'CONFIG_STM32H723', if_true: [
  files('dm_mc02_soc.c', 'dm_mc02_gpio.c', 'dm_mc02_gpio_vmstate.c', 'dm_mc02_gpio_exti_vmstate.c', 'dm_mc02_pwr_rcc.c', 'dm_mc02_pwr_rcc_vmstate.c', 'dm_stm32h7_clock_tree.c', 'dm_mc02_flash.c', 'dm_mc02_flash_vmstate.c', 'dm_mc02_fmc.c', 'dm_mc02_fmc_vmstate.c', 'dm_mc02_tim2.c', 'dm_mc02_tim2_vmstate.c', 'dm_mc02_syscfg.c', 'dm_mc02_syscfg_vmstate.c', 'dm_mc02_exti.c', 'dm_mc02_exti_vmstate.c', 'dm_mc02_dma.c', 'dm_mc02_dma_endpoint.c', 'dm_mc02_dma_vmstate.c', 'dm_mc02_dma_subsystem_vmstate.c', 'dm_mc02_dmamux_vmstate.c', 'dm_mc02_uart.c', 'dm_mc02_uart_vmstate.c', 'dm_mc02_adc.c', 'dm_mc02_adc_vmstate.c', 'dm_mc02_adc_common.c', 'dm_mc02_adc_common_vmstate.c', 'dm_mc02_adc_pair_vmstate.c', 'dm_mc02_trigger.c', 'dm_mc02_dbgmcu.c', 'dm_mc02_dbgmcu_vmstate.c', 'dm_mc02_rng.c', 'dm_mc02_rng_vmstate.c', 'dm_mc02_crc.c', 'dm_mc02_crc_vmstate.c', 'dm_mc02_ospi.c', 'dm_mc02_ospi_state.c', 'dm_mc02_ospi_vmstate.c', 'dm_mc02_ssi_nor.c', 'dm_mc02_iwdg.c', 'dm_mc02_iwdg_timing.c', 'dm_mc02_iwdg_vmstate.c', 'dm_mc02_wwdg.c', 'dm_mc02_wwdg_timing.c', 'dm_mc02_wwdg_vmstate.c', 'dm_mc02_cordic.c', 'dm_mc02_cordic_vmstate.c', 'dm_mc02_spi.c', 'dm_mc02_spi_vmstate.c'),
  meson.project_source_root() / '..' / '..' / 'cosim' / 'dm_nor_flash_persistence.c',
])
arm_ss.add(when: 'CONFIG_DM_MC02', if_true: [
  files('dm_mc02.c', 'dm_mc02_board.c', 'dm_mc02_board_reset.c', 'dm_mc02_cosim_link.c', 'dm_mc02_power.c', 'dm_mc02_power_vmstate.c', 'dm_mc02_fdcan.c', 'dm_mc02_fdcan_vmstate.c', 'dm_mc02_fdcan_msg_ram_link_vmstate.c', 'dm_mc02_usb.c', 'dm_mc02_bmi088_signal.c', 'dm_mc02_bmi088.c', 'dm_mc02_bmi088_vmstate.c', 'dm_mc02_bmi088_spi.c', 'dm_mc02_bmi088_spi_vmstate.c', 'dm_mc02_bmi088_spi_link.c', 'dm_mc02_bmi088_spi_link_vmstate.c', 'dm_mc02_reset_input.c'),
  meson.project_source_root() / '..' / '..' / 'cosim' / 'dm_mc02_wire.c',
  meson.project_source_root() / '..' / '..' / 'cosim' / 'dm_mc02_v2_wire.c',
  meson.project_source_root() / '..' / '..' / 'cosim' / 'dm_mc02_v2_payload.c',
])
arm_ss.add(when: 'CONFIG_STM32H723_USB_HOST', if_true: files('stm32h723_usb_host.c'))
arm_ss.add(when: 'CONFIG_ARM_VIRT', if_true: files('virt.c'))
arm_ss.add(when: 'CONFIG_ACPI', if_true: files('virt-acpi-build.c'))
arm_ss.add(when: 'CONFIG_DIGIC', if_true: files('digic_boards.c'))
arm_ss.add(when: 'CONFIG_EMCRAFT_SF2', if_true: files('msf2-som.c'))
arm_ss.add(when: 'CONFIG_HIGHBANK', if_true: files('highbank.c'))
arm_ss.add(when: 'CONFIG_INTEGRATOR', if_true: files('integratorcp.c'))
arm_ss.add(when: 'CONFIG_MAINSTONE', if_true: files('mainstone.c'))
arm_ss.add(when: 'CONFIG_MICROBIT', if_true: files('microbit.c'))
arm_ss.add(when: 'CONFIG_MUSICPAL', if_true: files('musicpal.c'))
arm_ss.add(when: 'CONFIG_NETDUINO2', if_true: files('netduino2.c'))
arm_ss.add(when: 'CONFIG_NETDUINOPLUS2', if_true: files('netduinoplus2.c'))
arm_ss.add(when: 'CONFIG_OLIMEX_STM32_H405', if_true: files('olimex-stm32-h405.c'))
arm_ss.add(when: 'CONFIG_NPCM7XX', if_true: files('npcm7xx.c', 'npcm7xx_boards.c'))
arm_ss.add(when: 'CONFIG_NSERIES', if_true: files('nseries.c'))
arm_ss.add(when: 'CONFIG_SX1', if_true: files('omap_sx1.c'))
arm_ss.add(when: 'CONFIG_CHEETAH', if_true: files('palm.c'))
arm_ss.add(when: 'CONFIG_GUMSTIX', if_true: files('gumstix.c'))
arm_ss.add(when: 'CONFIG_SPITZ', if_true: files('spitz.c'))
arm_ss.add(when: 'CONFIG_Z2', if_true: files('z2.c'))
arm_ss.add(when: 'CONFIG_REALVIEW', if_true: files('realview.c'))
arm_ss.add(when: 'CONFIG_SBSA_REF', if_true: files('sbsa-ref.c'))
arm_ss.add(when: 'CONFIG_STELLARIS', if_true: files('stellaris.c'))
arm_ss.add(when: 'CONFIG_STM32VLDISCOVERY', if_true: files('stm32vldiscovery.c'))
arm_ss.add(when: 'CONFIG_COLLIE', if_true: files('collie.c'))
arm_ss.add(when: 'CONFIG_VERSATILE', if_true: files('versatilepb.c'))
arm_ss.add(when: 'CONFIG_VEXPRESS', if_true: files('vexpress.c'))
arm_ss.add(when: 'CONFIG_ZYNQ', if_true: files('xilinx_zynq.c'))
arm_ss.add(when: 'CONFIG_SABRELITE', if_true: files('sabrelite.c'))

arm_ss.add(when: 'CONFIG_ARM_V7M', if_true: files('armv7m.c'))
arm_ss.add(when: 'CONFIG_EXYNOS4', if_true: files('exynos4210.c'))
arm_ss.add(when: 'CONFIG_PXA2XX', if_true: files('pxa2xx.c', 'pxa2xx_gpio.c', 'pxa2xx_pic.c'))
arm_ss.add(when: 'CONFIG_DIGIC', if_true: files('digic.c'))
arm_ss.add(when: 'CONFIG_OMAP', if_true: files('omap1.c', 'omap2.c'))
arm_ss.add(when: 'CONFIG_STRONGARM', if_true: files('strongarm.c'))
arm_ss.add(when: 'CONFIG_ALLWINNER_A10', if_true: files('allwinner-a10.c', 'cubieboard.c'))
arm_ss.add(when: 'CONFIG_ALLWINNER_H3', if_true: files('allwinner-h3.c', 'orangepi.c'))
arm_ss.add(when: 'CONFIG_ALLWINNER_R40', if_true: files('allwinner-r40.c', 'bananapi_m2u.c'))
arm_ss.add(when: 'CONFIG_RASPI', if_true: files('bcm2836.c', 'raspi.c'))
arm_ss.add(when: 'CONFIG_STM32F100_SOC', if_true: files('stm32f100_soc.c'))
arm_ss.add(when: 'CONFIG_STM32F205_SOC', if_true: files('stm32f205_soc.c'))
arm_ss.add(when: 'CONFIG_STM32F405_SOC', if_true: files('stm32f405_soc.c'))
arm_ss.add(when: 'CONFIG_XLNX_ZYNQMP_ARM', if_true: files('xlnx-zynqmp.c', 'xlnx-zcu102.c'))
arm_ss.add(when: 'CONFIG_XLNX_VERSAL', if_true: files('xlnx-versal.c', 'xlnx-versal-virt.c'))
arm_ss.add(when: 'CONFIG_FSL_IMX25', if_true: files('fsl-imx25.c', 'imx25_pdk.c'))
arm_ss.add(when: 'CONFIG_FSL_IMX31', if_true: files('fsl-imx31.c', 'kzm.c'))
arm_ss.add(when: 'CONFIG_FSL_IMX6', if_true: files('fsl-imx6.c'))
arm_ss.add(when: 'CONFIG_ASPEED_SOC', if_true: files(
  'aspeed.c',
  'aspeed_soc_common.c',
  'aspeed_ast2400.c',
  'aspeed_ast2600.c',
  'aspeed_ast10x0.c',
  'aspeed_eeprom.c',
  'fby35.c'))
arm_ss.add(when: 'CONFIG_MPS2', if_true: files('mps2.c'))
arm_ss.add(when: 'CONFIG_MPS2', if_true: files('mps2-tz.c'))
arm_ss.add(when: 'CONFIG_MSF2', if_true: files('msf2-soc.c'))
arm_ss.add(when: 'CONFIG_MUSCA', if_true: files('musca.c'))
arm_ss.add(when: 'CONFIG_ARMSSE', if_true: files('armsse.c'))
arm_ss.add(when: 'CONFIG_FSL_IMX7', if_true: files('fsl-imx7.c', 'mcimx7d-sabre.c'))
arm_ss.add(when: 'CONFIG_ARM_SMMUV3', if_true: files('smmuv3.c'))
arm_ss.add(when: 'CONFIG_FSL_IMX6UL', if_true: files('fsl-imx6ul.c', 'mcimx6ul-evk.c'))
arm_ss.add(when: 'CONFIG_NRF51_SOC', if_true: files('nrf51_soc.c'))
arm_ss.add(when: 'CONFIG_XEN', if_true: files('xen_arm.c'))

system_ss.add(when: 'CONFIG_ARM_SMMUV3', if_true: files('smmu-common.c'))
system_ss.add(when: 'CONFIG_EXYNOS4', if_true: files('exynos4_boards.c'))
system_ss.add(when: 'CONFIG_RASPI', if_true: files('bcm2835_peripherals.c'))
system_ss.add(when: 'CONFIG_TOSA', if_true: files('tosa.c'))

hw_arch += {'arm': arm_ss}
