
testblock = declare_dependency(dependencies: [block], sources: 'iothread.c')

tests = {
  'check-block-qdict': [],
  'check-qdict': [],
  'check-qnum': [],
  'check-qstring': [],
  'check-qlist': [],
  'check-qnull': [],
  'check-qobject': [],
  'check-qjson': [],
  'check-qlit': [],
  'test-error-report': [],
  'test-qobject-output-visitor': [testqapi],
  'test-clone-visitor': [testqapi],
  'test-qobject-input-visitor': [testqapi],
  'test-forward-visitor': [testqapi],
  'test-string-input-visitor': [testqapi],
  'test-string-output-visitor': [testqapi],
  'test-opts-visitor': [testqapi],
  'test-visitor-serialization': [testqapi],
  'test-bitmap': [],
  'test-resv-mem': [],
  # all code tested by test-x86-topo is inside topology.h
  'test-x86-topo': [],
  'test-cutils': [],
  'test-div128': [],
  'test-shift128': [],
  'test-mul64': [],
  # all code tested by test-int128 is inside int128.h
  'test-int128': [],
  'rcutorture': [],
  'test-rcu-list': [],
  'test-rcu-simpleq': [],
  'test-rcu-tailq': [],
  'test-rcu-slist': [],
  'test-qdist': [],
  'test-qht': [],
  'test-qtree': [],
  'test-bitops': [],
  'test-bitcnt': [],
  'test-qgraph': ['../qtest/libqos/qgraph.c'],
  'check-qom-interface': [qom],
  'check-qom-proplist': [qom],
  'test-qemu-opts': [],
  'test-keyval': [testqapi],
  'test-logging': [],
  'test-uuid': [],
  'ptimer-test': ['ptimer-test-stubs.c', meson.project_source_root() / 'hw/core/ptimer.c'],
  'test-qapi-util': [],
  'test-interval-tree': [],
  'test-xs-node': [qom],
  'test-virtio-dmabuf': [meson.project_source_root() / 'hw/display/virtio-dmabuf.c'],
  'test-dm-message-ram': [
    meson.project_source_root() / 'hw/misc/dm_message_ram.c',
    'test-dm-message-ram-stubs.c',
    hwcore,
  ],
  'test-dm-soc-memory': [
    meson.project_source_root() / 'hw/arm/dm_mc02_soc.c',
    'test-dm-soc-memory-stubs.c',
    hwcore,
  ],
  'test-dm-wwdg-timing': [
    meson.project_source_root() / 'hw/arm/dm_mc02_wwdg_timing.c',
    'test-dm-wwdg-timing.c',
    hwcore,
  ],
  'test-dm-wwdg-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_wwdg_vmstate.c',
    'test-dm-wwdg-vmstate-stubs.c',
    migration,
    io,
  ],
  'test-dm-usb-endpoint-queue': [meson.project_source_root() / 'hw/usb/dm_usb_endpoint_queue.c'],
  'test-dm-usb-control': [meson.project_source_root() / 'hw/usb/dm_usb_control.c'],
  'test-dm-usb-control-vmstate': [
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_vmstate.c',
    meson.project_source_root() / 'hw/usb/dm_usb_control_vmstate.c',
    migration,
    io,
  ],
  'test-dm-usb-transaction': [
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
  ],
  'test-dm-usb-host': [
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host.c',
  ],
  'test-dm-usb-host-port': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_port.c',
  ],
  'test-dm-usb-host-pio-fifo': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_pio_fifo.c',
  ],
  'test-dm-stm32h7-otg-host': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_port.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_pio_fifo.c',
    meson.project_source_root() / 'hw/usb/dm_stm32h7_otg_host.c',
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_device.c',
  ],
  'test-dm-usb-host-channel-control': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_port.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_pio_fifo.c',
    meson.project_source_root() / 'hw/usb/dm_stm32h7_otg_host.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_channel_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_channel_transport.c',
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
  ],
  'test-dm-usb-host-channel-data-path': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_port.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_pio_fifo.c',
    meson.project_source_root() / 'hw/usb/dm_stm32h7_otg_host.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_channel_data_path.c',
  ],
  'test-dm-usb-host-qemu-memory': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_qemu_memory.c',
    hwcore,
  ],
  'test-dm-usb-host-channel-transport': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_port.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_pio_fifo.c',
    meson.project_source_root() / 'hw/usb/dm_stm32h7_otg_host.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_channel_data_path.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_channel_transport.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_qemu_memory.c',
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
    hwcore,
  ],
  'test-dm-usb-host-qemu-completion-scheduler': [
    meson.project_source_root() / 'hw/usb/dm_usb_host_port.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_pio_fifo.c',
    meson.project_source_root() / 'hw/usb/dm_stm32h7_otg_host.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_qemu_completion_scheduler.c',
    hwcore,
  ],
  'test-dm-usb-dwc2-device': [
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_device.c',
  ],
  'test-dm-usb-dwc2-vmstate': [
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_vmstate.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_device.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_device_vmstate.c',
    migration,
    io,
  ],
  'test-dm-usb-dwc2-control-link-vmstate': [
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_vmstate.c',
    meson.project_source_root() / 'hw/usb/dm_usb_control_vmstate.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_device.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_device_vmstate.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_control_vmstate.c',
    migration,
    io,
  ],
  'test-dm-usb-qemu-adapter': [
    meson.project_source_root() / 'hw/usb/dm_usb_qemu_adapter.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_pio_fifo.c',
    meson.project_source_root() / 'hw/usb/dm_stm32h7_otg_host.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_channel_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_channel_transport.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_qemu_port.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_qemu_transport.c',
    meson.project_source_root() / 'hw/usb/dm_usb_control.c',
    meson.project_source_root() / 'hw/usb/dm_usb_transaction.c',
    meson.project_source_root() / 'hw/usb/dm_usb_host_port.c',
    meson.project_source_root() / 'hw/usb/dm_usb_dwc2_device.c',
    meson.project_source_root() / 'hw/usb/core.c',
    meson.project_source_root() / 'hw/usb/bus.c',
    qom,
    hwcore,
  ],
  'test-dm-can-bus-adapter': [
    meson.project_source_root() / 'hw/net/can/dm_can_bus_adapter.c',
    meson.project_source_root() / 'net/can/can_core.c',
    qom,
  ],
  'test-dm-fdcan-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_fdcan.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_fdcan_vmstate.c',
    meson.project_source_root() / 'hw/net/can/dm_can_bus_adapter.c',
    meson.project_source_root() / 'net/can/can_core.c',
    'test-dm-fdcan-vmstate-stubs.c',
    migration,
    io,
    qom,
    chardev,
    hwcore,
  ],
  'test-dm-fdcan-msg-ram-link': [
    meson.project_source_root() / 'hw/arm/dm_mc02_fdcan.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_fdcan_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_fdcan_msg_ram_link_vmstate.c',
    meson.project_source_root() / 'hw/net/can/dm_can_bus_adapter.c',
    meson.project_source_root() / 'net/can/can_core.c',
    'test-dm-fdcan-vmstate-stubs.c',
    migration,
    io,
    qom,
    chardev,
    hwcore,
  ],
  'test-dm-cordic-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_cordic_vmstate.c',
    migration,
    io,
  ],
  'test-dm-crc-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_crc_vmstate.c',
    migration,
    io,
  ],
  'test-dm-rng-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_rng_vmstate.c',
    migration,
    io,
  ],
  'test-dm-dbgmcu-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_dbgmcu_vmstate.c',
    migration,
    io,
  ],
  'test-dm-syscfg-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_syscfg_vmstate.c',
    migration,
    io,
  ],
  'test-dm-fmc-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_fmc_vmstate.c',
    migration,
    io,
  ],
  'test-dm-flash-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_flash_vmstate.c',
    migration,
    io,
  ],
  'test-dm-exti-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_exti_vmstate.c',
    migration,
    io,
  ],
  'test-dm-dmamux-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_dmamux_vmstate.c',
    migration,
    io,
  ],
  'test-dm-dma-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_dma.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_dma_endpoint.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_dma_vmstate.c',
    'test-dm-dma-vmstate-stubs.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-dma-subsystem-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_dma.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_dma_endpoint.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_dma_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_dmamux_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_dma_subsystem_vmstate.c',
    'test-dm-dma-vmstate-stubs.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-gpio-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_gpio_vmstate.c',
    migration,
    io,
  ],
  'test-dm-gpio-exti-link-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_gpio.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_gpio_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_syscfg.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_syscfg_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_exti.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_exti_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_gpio_exti_vmstate.c',
    'test-dm-gpio-exti-link-vmstate-stubs.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-tim2-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_tim2_vmstate.c',
    'test-dm-tim2-vmstate-stubs.c',
    migration,
    io,
  ],
  'test-dm-adc-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_adc_vmstate.c',
    'test-dm-adc-vmstate-stubs.c',
    migration,
    io,
  ],
  'test-dm-adc-common': [
    meson.project_source_root() / 'hw/arm/dm_mc02_adc_common.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_adc_common_vmstate.c',
    'test-dm-adc-common-stubs.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-adc-pair-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_adc_common_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_adc_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_adc_pair_vmstate.c',
    'test-dm-adc-pair-vmstate-stubs.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-spi-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_spi_vmstate.c',
    'test-dm-spi-vmstate-stubs.c',
    migration,
    io,
  ],
  'test-dm-uart-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_uart_vmstate.c',
    'test-dm-uart-vmstate-stubs.c',
    migration,
    io,
  ],
  'test-dm-pwr-rcc-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_pwr_rcc.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_pwr_rcc_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_stm32h7_clock_tree.c',
    'test-dm-pwr-rcc-vmstate-stubs.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-bmi088-spi-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_bmi088_spi_vmstate.c',
    migration,
    io,
  ],
  'test-dm-bmi088-spi-link-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_spi_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_bmi088_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_bmi088_spi_vmstate.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_bmi088_spi_link_vmstate.c',
    'test-dm-bmi088-spi-link-vmstate-stubs.c',
    migration,
    io,
  ],
  'test-dm-bmi088-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_bmi088_vmstate.c',
    migration,
    io,
  ],
  'test-dm-adc-runtime-sync': [
    meson.project_source_root() / 'hw/arm/dm_mc02_adc.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_dma_endpoint.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_adc_vmstate.c',
    'test-dm-adc-runtime-sync-stubs.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-iwdg-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_iwdg_vmstate.c',
    'test-dm-iwdg-vmstate-stubs.c',
    migration,
    io,
  ],
  'test-dm-iwdg-timing': [
    meson.project_source_root() / 'hw/arm/dm_mc02_iwdg_timing.c',
  ],
  'test-dm-power-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_power.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_power_vmstate.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-board-reset': [
    meson.project_source_root() / 'hw/arm/dm_mc02_board_reset.c',
    'test-dm-board-reset-stubs.c',
  ],
  'test-dm-reset-input': [
    meson.project_source_root() / 'hw/arm/dm_mc02_reset_input.c',
    meson.project_source_root() / 'hw/core/gpio.c',
    hwcore,
  ],
  'test-dm-w25q64': [
    meson.project_source_root() / 'hw/block/dm_w25q64.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_ssi_nor.c',
    meson.project_source_root() / 'hw/ssi/ssi.c',
    meson.project_source_root() / 'hw/core/gpio.c',
    meson.project_source_root() / '..' / '..' / 'cosim/dm_nor_flash.c',
    migration,
    io,
    hwcore,
  ],
  'test-dm-ospi-vmstate': [
    meson.project_source_root() / 'hw/arm/dm_mc02_ospi_state.c',
    meson.project_source_root() / 'hw/arm/dm_mc02_ospi_vmstate.c',
    'test-dm-ospi-vmstate-stubs.c',
    migration,
    io,
  ],
}

if have_system or have_tools
  tests += {
    'test-qmp-event': [testqapi],
  }

  if seccomp.found()
    tests += {'test-seccomp': ['../../system/qemu-seccomp.c', seccomp]}
  endif
endif

if have_block
  tests += {
    'test-coroutine': [testblock],
    'test-aio': [testblock],
    'test-aio-multithread': [testblock],
    'test-throttle': [testblock],
    'test-thread-pool': [testblock],
    'test-hbitmap': [testblock],
    'test-bdrv-drain': [testblock],
    'test-bdrv-graph-mod': [testblock],
    'test-blockjob': [testblock],
    'test-blockjob-txn': [testblock],
    'test-block-backend': [testblock],
    'test-block-iothread': [testblock],
    'test-write-threshold': [testblock],
    'test-crypto-hash': [crypto],
    'test-crypto-hmac': [crypto],
    'test-crypto-cipher': [crypto],
    'test-crypto-akcipher': [crypto],
    'test-crypto-secret': [crypto, keyutils],
    'test-crypto-der': [crypto],
    'test-authz-simple': [authz],
    'test-authz-list': [authz],
    'test-authz-listfile': [authz],
    'test-io-task': [testblock],
    'test-io-channel-socket': ['socket-helpers.c', 'io-channel-helpers.c', io],
    'test-io-channel-file': ['io-channel-helpers.c', io],
    'test-io-channel-command': ['io-channel-helpers.c', io],
    'test-io-channel-buffer': ['io-channel-helpers.c', io],
    'test-io-channel-null': [io],
    'test-crypto-ivgen': [io],
    'test-crypto-afsplit': [io],
    'test-crypto-block': [io],
  }
  if gnutls.found() and \
     tasn1.found() and \
     targetos != 'windows'
    tests += {
      'test-crypto-tlscredsx509': ['crypto-tls-x509-helpers.c', 'pkix_asn1_tab.c',
                                   tasn1, crypto, gnutls],
      'test-crypto-tlssession': ['crypto-tls-x509-helpers.c', 'pkix_asn1_tab.c', 'crypto-tls-psk-helpers.c',
                                 tasn1, crypto, gnutls],
      'test-io-channel-tls': ['io-channel-helpers.c', 'crypto-tls-x509-helpers.c', 'pkix_asn1_tab.c',
                              tasn1, io, crypto, gnutls]}
  endif
  if pam.found()
    tests += {'test-authz-pam': [authz]}
  endif
  if xts == 'private'
    tests += {'test-crypto-xts': [crypto, io]}
  endif
  if targetos != 'windows'
    tests += {
      'test-image-locking': [testblock],
      'test-nested-aio-poll': [testblock],
    }
  endif
  if config_host_data.get('CONFIG_REPLICATION')
    tests += {'test-replication': [testblock]}
  endif
  if nettle.found() or gcrypt.found()
    tests += {'test-crypto-pbkdf': [io]}
  endif
endif

if have_system
  tests += {
    'test-iov': [],
    'test-qmp-cmds': [testqapi],
    'test-xbzrle': [migration],
    'test-timed-average': [],
    'test-util-sockets': ['socket-helpers.c'],
    'test-base64': [],
    'test-bufferiszero': [],
    'test-smp-parse': [qom, meson.project_source_root() / 'hw/core/machine-smp.c'],
    'test-vmstate': [migration, io],
    'test-yank': ['socket-helpers.c', qom, io, chardev]
  }
  if config_host_data.get('CONFIG_INOTIFY1')
    tests += {'test-util-filemonitor': []}
  endif

  # Some tests: test-char, test-qdev-global-props, and test-qga,
  # are not runnable under TSan due to a known issue.
  # https://github.com/google/sanitizers/issues/1116
  if not get_option('tsan')
    if targetos != 'windows'
        tests += {
          'test-char': ['socket-helpers.c', qom, io, chardev]
        }
    endif

    tests += {
      'test-qdev-global-props': [qom, hwcore]
    }
  endif
endif

if have_ga and targetos == 'linux'
  tests += {'test-qga': ['../qtest/libqmp.c']}
  test_deps += {'test-qga': qga}
endif

test_env = environment()
test_env.set('G_TEST_SRCDIR', meson.current_source_dir())
test_env.set('G_TEST_BUILDDIR', meson.current_build_dir())

slow_tests = {
  'test-crypto-tlscredsx509': 45,
  'test-crypto-tlssession': 45
}

foreach test_name, extra: tests
  src = [test_name + '.c']
  deps = [qemuutil]
  if extra.length() > 0
    # use a sourceset to quickly separate sources and deps
    test_ss = ss.source_set()
    test_ss.add(extra)
    src += test_ss.all_sources()
    deps += test_ss.all_dependencies()
  endif
  exe = executable(test_name, src, genh, dependencies: deps)

  test(test_name, exe,
       depends: test_deps.get(test_name, []),
       env: test_env,
       args: ['--tap', '-k'],
       protocol: 'tap',
       timeout: slow_tests.get(test_name, 30),
       priority: slow_tests.get(test_name, 30),
       suite: ['unit'])
endforeach
