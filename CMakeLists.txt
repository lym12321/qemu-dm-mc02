cmake_minimum_required(VERSION 3.16)
project(dm_mc02_qemu_probe VERSION 0.1.0 LANGUAGES C)

find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB2 REQUIRED IMPORTED_TARGET glib-2.0)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_executable(dm_mc02_probe
    src/dm_mc02_machine.c
)
target_include_directories(dm_mc02_probe PRIVATE include)
target_compile_options(dm_mc02_probe PRIVATE -Wall -Wextra -Wpedantic)

# The QEMU-native source needs QEMU's internal headers and is built from the
# pinned QEMU source tree; this target only keeps the integration source visible
# in IDEs and source distributions.
add_custom_target(dm_mc02_qemu_native_source SOURCES
    qemu/upstream/hw/arm/dm_mc02.c
)

add_executable(dm_mc02_wire_smoke
    tests/wire_smoke.c
    cosim/dm_mc02_wire.c
)
target_include_directories(dm_mc02_wire_smoke PRIVATE cosim)
target_compile_options(dm_mc02_wire_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_v2_wire_smoke
    tests/v2_wire_smoke.c
    cosim/dm_mc02_wire.c
    cosim/dm_mc02_v2_wire.c
)
target_include_directories(dm_mc02_v2_wire_smoke PRIVATE cosim)
target_compile_options(dm_mc02_v2_wire_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_v2_wire_vectors
    tests/v2_wire_vectors.c
    cosim/dm_mc02_wire.c
    cosim/dm_mc02_v2_wire.c
)
target_include_directories(dm_mc02_v2_wire_vectors PRIVATE cosim)
target_compile_options(dm_mc02_v2_wire_vectors PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_v2_payload_smoke
    tests/v2_payload_smoke.c
    cosim/dm_mc02_wire.c
    cosim/dm_mc02_v2_payload.c
)
target_include_directories(dm_mc02_v2_payload_smoke PRIVATE cosim)
target_compile_options(dm_mc02_v2_payload_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_v2_payload_vectors
    tests/v2_payload_vectors.c
    cosim/dm_mc02_wire.c
    cosim/dm_mc02_v2_payload.c
)
target_include_directories(dm_mc02_v2_payload_vectors PRIVATE cosim)
target_compile_options(dm_mc02_v2_payload_vectors PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_v2_adc_payload_smoke
    tests/v2_adc_payload_smoke.c
    cosim/dm_mc02_wire.c
    cosim/dm_mc02_v2_payload.c
)
target_include_directories(dm_mc02_v2_adc_payload_smoke PRIVATE cosim)
target_compile_options(dm_mc02_v2_adc_payload_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_library(dm_mc02_protocol STATIC
    cosim/dm_mc02_wire.c
    cosim/dm_mc02_protocol.c
)
target_include_directories(dm_mc02_protocol PUBLIC cosim)
target_compile_options(dm_mc02_protocol PRIVATE -Wall -Wextra -Wpedantic)

add_library(dm_mc02_transport STATIC
    cosim/dm_mc02_transport.c
)
target_include_directories(dm_mc02_transport PUBLIC cosim)
target_link_libraries(dm_mc02_transport PUBLIC dm_mc02_protocol)
target_compile_options(dm_mc02_transport PRIVATE -Wall -Wextra -Wpedantic)

add_executable(dm_mc02_cosim_smoke tests/cosim_smoke.c)
target_link_libraries(dm_mc02_cosim_smoke PRIVATE dm_mc02_protocol m)
target_compile_options(dm_mc02_cosim_smoke PRIVATE -Wall -Wextra -Wpedantic)

add_executable(dm_mc02_transport_smoke tests/transport_smoke.c)
target_link_libraries(dm_mc02_transport_smoke PRIVATE dm_mc02_transport)
target_compile_options(dm_mc02_transport_smoke PRIVATE -Wall -Wextra -Wpedantic)

add_executable(dm_usb_host_descriptor_smoke
    tests/usb_host_descriptor_smoke.c
    firmware/dm_usb_host_descriptor.c
)
target_include_directories(dm_usb_host_descriptor_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_descriptor_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_pipe_smoke
    tests/usb_host_pipe_smoke.c
    firmware/dm_usb_host_pipe.c
)
target_include_directories(dm_usb_host_pipe_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_pipe_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_retry_smoke
    tests/usb_host_retry_smoke.c
    firmware/dm_usb_host_retry.c
)
target_include_directories(dm_usb_host_retry_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_retry_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_pipe_smoke
    tests/stm32h7_usb_host_pipe_smoke.c
    firmware/dm_stm32h7_usb_host_pipe.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_channel_completion.c
)
target_include_directories(dm_stm32h7_usb_host_pipe_smoke PRIVATE firmware)
target_compile_options(dm_stm32h7_usb_host_pipe_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_control_nak_smoke
    tests/stm32h7_usb_host_control_nak_smoke.c
    firmware/dm_stm32h7_usb_host_control.c
)
target_include_directories(dm_stm32h7_usb_host_control_nak_smoke PRIVATE
                           firmware)
target_compile_options(dm_stm32h7_usb_host_control_nak_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_pipe_async_smoke
    tests/stm32h7_usb_host_pipe_async_smoke.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_channel_completion.c
    firmware/dm_stm32h7_usb_host_pipe.c
)
target_include_directories(dm_stm32h7_usb_host_pipe_async_smoke PRIVATE firmware)
target_compile_options(dm_stm32h7_usb_host_pipe_async_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_pipe_async_dispatch_smoke
    tests/stm32h7_usb_host_pipe_async_dispatch_smoke.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_channel_completion.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_stm32h7_usb_host_pipe.c
    firmware/dm_stm32h7_usb_host_pipe_async_dispatch.c
)
target_include_directories(dm_stm32h7_usb_host_pipe_async_dispatch_smoke PRIVATE
                           firmware)
target_compile_options(dm_stm32h7_usb_host_pipe_async_dispatch_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_async_dispatch_contract_smoke
    tests/stm32h7_usb_host_async_dispatch_contract_smoke.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_channel_completion.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_stm32h7_usb_host_pipe.c
    firmware/dm_stm32h7_usb_host_pipe_async_dispatch.c
)
target_include_directories(dm_stm32h7_usb_host_async_dispatch_contract_smoke PRIVATE
                           firmware)
target_compile_options(dm_stm32h7_usb_host_async_dispatch_contract_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_bulk_async_smoke
    tests/stm32h7_usb_host_bulk_async_smoke.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_channel_completion.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_stm32h7_usb_host_pipe.c
    firmware/dm_stm32h7_usb_host_pipe_async_dispatch.c
    firmware/dm_stm32h7_usb_host_bulk_async.c)
target_include_directories(dm_stm32h7_usb_host_bulk_async_smoke PRIVATE
                           firmware)
target_compile_options(dm_stm32h7_usb_host_bulk_async_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_endpoint_state_smoke
    tests/usb_host_endpoint_state_smoke.c
    firmware/dm_usb_host_endpoint_state.c
)
target_include_directories(dm_usb_host_endpoint_state_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_endpoint_state_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_channel_allocator_smoke
    tests/usb_host_channel_allocator_smoke.c
    firmware/dm_usb_host_channel_allocator.c
)
target_include_directories(dm_usb_host_channel_allocator_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_channel_allocator_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_channel_operation_smoke
    tests/usb_host_channel_operation_smoke.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_channel_operation.c
)
target_include_directories(dm_usb_host_channel_operation_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_channel_operation_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_channel_completion_smoke
    tests/usb_host_channel_completion_smoke.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_channel_completion.c
)
target_include_directories(dm_usb_host_channel_completion_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_channel_completion_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_bulk_smoke
    tests/usb_host_bulk_smoke.c
    firmware/dm_usb_host_bulk.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_retry.c
)
target_include_directories(dm_usb_host_bulk_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_bulk_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_bulk_smoke
    tests/stm32h7_usb_host_bulk_smoke.c
    firmware/dm_usb_host_bulk.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_retry.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_channel_completion.c
    firmware/dm_stm32h7_usb_host_pipe.c
    firmware/dm_stm32h7_usb_host_bulk.c
)
target_include_directories(dm_stm32h7_usb_host_bulk_smoke PRIVATE firmware)
target_compile_options(dm_stm32h7_usb_host_bulk_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_periodic_schedule_smoke
    tests/usb_host_periodic_schedule_smoke.c
    firmware/dm_usb_host_periodic_schedule.c
)
target_include_directories(dm_usb_host_periodic_schedule_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_periodic_schedule_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_periodic_poller_smoke
    tests/usb_host_periodic_poller_smoke.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_periodic_schedule.c
    firmware/dm_usb_host_periodic_poller.c
    firmware/dm_usb_host_retry.c
)
target_include_directories(dm_usb_host_periodic_poller_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_periodic_poller_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_sof_clock_smoke
    tests/usb_host_sof_clock_smoke.c
    firmware/dm_usb_host_sof_clock.c
)
target_include_directories(dm_usb_host_sof_clock_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_sof_clock_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_periodic_sof_smoke
    tests/stm32h7_usb_host_periodic_sof_smoke.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_periodic_schedule.c
    firmware/dm_usb_host_periodic_poller.c
    firmware/dm_usb_host_retry.c
    firmware/dm_usb_host_sof_clock.c
    firmware/dm_stm32h7_usb_host_sof.c
    firmware/dm_stm32h7_usb_host_periodic_sof.c
)
target_include_directories(dm_stm32h7_usb_host_periodic_sof_smoke PRIVATE firmware)
target_compile_options(dm_stm32h7_usb_host_periodic_sof_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_periodic_smoke
    tests/stm32h7_usb_host_periodic_smoke.c
    firmware/dm_usb_host_channel_allocator.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_periodic_schedule.c
    firmware/dm_usb_host_periodic_poller.c
    firmware/dm_usb_host_retry.c
    firmware/dm_stm32h7_usb_host_pipe.c
    firmware/dm_usb_host_channel_operation.c
    firmware/dm_usb_host_channel_completion.c
    firmware/dm_stm32h7_usb_host_periodic.c
)
target_include_directories(dm_stm32h7_usb_host_periodic_smoke PRIVATE firmware)
target_compile_options(dm_stm32h7_usb_host_periodic_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_usb_host_periodic_registry_smoke
    tests/usb_host_periodic_registry_smoke.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_periodic_schedule.c
    firmware/dm_usb_host_periodic_poller.c
    firmware/dm_usb_host_retry.c
    firmware/dm_usb_host_periodic_registry.c
)
target_include_directories(dm_usb_host_periodic_registry_smoke PRIVATE firmware)
target_compile_options(dm_usb_host_periodic_registry_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_periodic_registry_sof_smoke
    tests/stm32h7_usb_host_periodic_registry_sof_smoke.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_periodic_schedule.c
    firmware/dm_usb_host_periodic_poller.c
    firmware/dm_usb_host_periodic_registry.c
    firmware/dm_usb_host_retry.c
    firmware/dm_usb_host_sof_clock.c
    firmware/dm_stm32h7_usb_host_sof.c
    firmware/dm_stm32h7_usb_host_periodic_registry_sof.c
)
target_include_directories(dm_stm32h7_usb_host_periodic_registry_sof_smoke PRIVATE firmware)
target_compile_options(dm_stm32h7_usb_host_periodic_registry_sof_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_usb_host_sof_irq_smoke
    tests/stm32h7_usb_host_sof_irq_smoke.c
    firmware/dm_usb_host_endpoint_state.c
    firmware/dm_usb_host_periodic_schedule.c
    firmware/dm_usb_host_periodic_poller.c
    firmware/dm_usb_host_periodic_registry.c
    firmware/dm_usb_host_retry.c
    firmware/dm_usb_host_sof_clock.c
    firmware/dm_stm32h7_usb_host_sof.c
    firmware/dm_stm32h7_usb_host_periodic_registry_sof.c
    firmware/dm_stm32h7_usb_host_sof_irq.c
)
target_include_directories(dm_stm32h7_usb_host_sof_irq_smoke PRIVATE firmware)
target_compile_options(dm_stm32h7_usb_host_sof_irq_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_bmi088_timing_smoke tests/bmi088_timing_smoke.c)
target_include_directories(dm_mc02_bmi088_timing_smoke PRIVATE
                           qemu/upstream/hw/arm)
target_compile_options(dm_mc02_bmi088_timing_smoke PRIVATE
                       -Wall -Wextra -Wpedantic)

add_executable(dm_mc02_bmi088_fifo_smoke tests/bmi088_fifo_smoke.c)
target_include_directories(dm_mc02_bmi088_fifo_smoke PRIVATE
                           qemu/upstream/hw/arm)
target_compile_options(dm_mc02_bmi088_fifo_smoke PRIVATE
                       -Wall -Wextra -Wpedantic)

add_executable(dm_mc02_bmi088_signal_smoke
    tests/bmi088_signal_smoke.c
    qemu/upstream/hw/arm/dm_mc02_bmi088_signal.c)
target_include_directories(dm_mc02_bmi088_signal_smoke PRIVATE
                           qemu/upstream/hw/arm)
target_link_libraries(dm_mc02_bmi088_signal_smoke PRIVATE m)
target_compile_options(dm_mc02_bmi088_signal_smoke PRIVATE
                       -Wall -Wextra -Wpedantic)

add_executable(dm_mc02_bmi088_drift_smoke
    tests/bmi088_drift_smoke.c
    qemu/upstream/hw/arm/dm_mc02_bmi088_signal.c)
target_include_directories(dm_mc02_bmi088_drift_smoke PRIVATE
                           qemu/upstream/hw/arm)
target_link_libraries(dm_mc02_bmi088_drift_smoke PRIVATE m)
target_compile_options(dm_mc02_bmi088_drift_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_bmi088_chip_smoke
    tests/bmi088_chip_smoke.c
    qemu/upstream/hw/arm/dm_mc02_bmi088.c
    qemu/upstream/hw/arm/dm_mc02_bmi088_signal.c)
target_include_directories(dm_mc02_bmi088_chip_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_link_libraries(dm_mc02_bmi088_chip_smoke PRIVATE m PkgConfig::GLIB2)
set_property(TARGET dm_mc02_bmi088_chip_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_bmi088_chip_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -UNDEBUG)

add_executable(dm_mc02_bmi088_spi_smoke
    tests/bmi088_spi_smoke.c
    qemu/upstream/hw/arm/dm_mc02_bmi088_spi.c
    qemu/upstream/hw/arm/dm_mc02_bmi088.c
    qemu/upstream/hw/arm/dm_mc02_bmi088_signal.c)
target_include_directories(dm_mc02_bmi088_spi_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_link_libraries(dm_mc02_bmi088_spi_smoke PRIVATE m PkgConfig::GLIB2)
set_property(TARGET dm_mc02_bmi088_spi_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_bmi088_spi_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -UNDEBUG)

add_executable(dm_mc02_spi_target_smoke
    tests/spi_target_smoke.c
    qemu/upstream/hw/arm/dm_mc02_spi.c
    qemu/upstream/hw/arm/dm_mc02_dma.c
    qemu/upstream/hw/arm/dm_mc02_dma_endpoint.c
    qemu/upstream/hw/arm/dm_mc02_bmi088_spi.c
    qemu/upstream/hw/arm/dm_mc02_bmi088.c
    qemu/upstream/hw/arm/dm_mc02_bmi088_signal.c
    qemu/upstream/hw/arm/dm_mc02_spi_nor.c
    cosim/dm_nor_flash.c)
target_include_directories(dm_mc02_spi_target_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_link_libraries(dm_mc02_spi_target_smoke PRIVATE
                      m PkgConfig::GLIB2 pthread)
set_property(TARGET dm_mc02_spi_target_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_spi_target_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -UNDEBUG
                        -Wno-pointer-arith -Wno-sign-compare
                        -ffunction-sections)
target_link_options(dm_mc02_spi_target_smoke PRIVATE
                     -Wl,--gc-sections)

add_executable(dm_spi_nor_flash_smoke
    tests/spi_nor_flash_smoke.c
    qemu/upstream/hw/arm/dm_mc02_spi_nor.c
    cosim/dm_nor_flash.c)
target_include_directories(dm_spi_nor_flash_smoke PRIVATE
                           qemu/upstream/hw/arm cosim)
target_compile_options(dm_spi_nor_flash_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror -UNDEBUG)

add_executable(dm_nor_flash_smoke
    tests/nor_flash_smoke.c
    cosim/dm_nor_flash.c)
target_include_directories(dm_nor_flash_smoke PRIVATE cosim)
target_compile_options(dm_nor_flash_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror -UNDEBUG)

add_executable(dm_nor_flash_persistence_smoke
    tests/nor_flash_persistence_smoke.c
    cosim/dm_nor_flash_persistence.c)
target_include_directories(dm_nor_flash_persistence_smoke PRIVATE cosim)
target_compile_options(dm_nor_flash_persistence_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror -UNDEBUG)

add_executable(dm_mc02_board_profile_smoke
    tests/board_profile_smoke.c
    qemu/upstream/hw/arm/dm_mc02_board.c)
target_sources(dm_mc02_board_profile_smoke PRIVATE
               qemu/upstream/hw/arm/dm_mc02_soc.c)
target_include_directories(dm_mc02_board_profile_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_link_libraries(dm_mc02_board_profile_smoke PRIVATE
                       PkgConfig::GLIB2
                       "${CMAKE_CURRENT_SOURCE_DIR}/build/qemu/libqemuutil.a"
                       m)
set_property(TARGET dm_mc02_board_profile_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_board_profile_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -UNDEBUG
                        -ffunction-sections)
target_link_options(dm_mc02_board_profile_smoke PRIVATE
                     -Wl,--gc-sections)

add_executable(dm_mc02_soc_map_smoke
    tests/soc_map_smoke.c
    qemu/upstream/hw/arm/dm_mc02_soc.c)
target_include_directories(dm_mc02_soc_map_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_link_libraries(dm_mc02_soc_map_smoke PRIVATE
                       PkgConfig::GLIB2
                       "${CMAKE_CURRENT_SOURCE_DIR}/build/qemu/libqemuutil.a")
set_property(TARGET dm_mc02_soc_map_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_soc_map_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -UNDEBUG
                        -ffunction-sections)
target_link_options(dm_mc02_soc_map_smoke PRIVATE
                     -Wl,--gc-sections)

add_executable(dm_mc02_rng_smoke
    tests/rng_smoke.c
    qemu/upstream/hw/arm/dm_mc02_rng.c)
target_include_directories(dm_mc02_rng_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_link_libraries(dm_mc02_rng_smoke PRIVATE
                       PkgConfig::GLIB2
                       "${CMAKE_CURRENT_SOURCE_DIR}/build/qemu/libqemuutil.a")
set_property(TARGET dm_mc02_rng_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_rng_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -UNDEBUG
                        -ffunction-sections)
target_link_options(dm_mc02_rng_smoke PRIVATE
                     -Wl,--gc-sections)

add_executable(dm_mc02_trigger_smoke
    tests/trigger_smoke.c
    qemu/upstream/hw/arm/dm_mc02_trigger.c)
target_include_directories(dm_mc02_trigger_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_compile_options(dm_mc02_trigger_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_stm32h7_clock_tree_smoke
    tests/clock_tree_smoke.c
    qemu/upstream/hw/arm/dm_stm32h7_clock_tree.c)
target_include_directories(dm_stm32h7_clock_tree_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm)
target_compile_options(dm_stm32h7_clock_tree_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_uart_timing_smoke
    tests/uart_timing_smoke.c
    qemu/upstream/hw/char/dm_uart_timing.c)
target_include_directories(dm_uart_timing_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include)
set_property(TARGET dm_uart_timing_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_uart_timing_smoke PRIVATE
                       -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_dma_endpoint_smoke
    tests/dma_endpoint_smoke.c
    qemu/upstream/hw/arm/dm_mc02_dma_endpoint.c)
target_include_directories(dm_mc02_dma_endpoint_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/hw/arm)
target_compile_options(dm_mc02_dma_endpoint_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Werror)

add_executable(dm_mc02_ospi_dma_endpoint_smoke
    tests/ospi_dma_endpoint_smoke.c
    qemu/upstream/hw/arm/dm_mc02_ospi.c
    cosim/dm_nor_flash.c
    cosim/dm_nor_flash_persistence.c)
target_compile_definitions(dm_mc02_ospi_dma_endpoint_smoke PRIVATE
                            DM_MC02_OSPI_TEST_FIXTURE=1)
target_include_directories(dm_mc02_ospi_dma_endpoint_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu"
                           cosim)
target_link_libraries(dm_mc02_ospi_dma_endpoint_smoke PRIVATE
                      PkgConfig::GLIB2)
set_property(TARGET dm_mc02_ospi_dma_endpoint_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_ospi_dma_endpoint_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Wno-pointer-arith
                        -Wno-sign-compare -UNDEBUG -ffunction-sections)
target_link_options(dm_mc02_ospi_dma_endpoint_smoke PRIVATE
                     -Wl,--gc-sections)

add_executable(dm_mc02_ospi_dma_integration_smoke
    tests/ospi_dma_integration_smoke.c
    qemu/upstream/hw/arm/dm_mc02_dma.c
    qemu/upstream/hw/arm/dm_mc02_dma_endpoint.c
    qemu/upstream/hw/arm/dm_mc02_ospi.c
    cosim/dm_nor_flash.c
    cosim/dm_nor_flash_persistence.c)
target_compile_definitions(dm_mc02_ospi_dma_integration_smoke PRIVATE
                            DM_MC02_OSPI_TEST_FIXTURE=1)
target_include_directories(dm_mc02_ospi_dma_integration_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu"
                           cosim)
target_link_libraries(dm_mc02_ospi_dma_integration_smoke PRIVATE
                      PkgConfig::GLIB2)
set_property(TARGET dm_mc02_ospi_dma_integration_smoke PROPERTY
             C_EXTENSIONS ON)
target_compile_options(dm_mc02_ospi_dma_integration_smoke PRIVATE
                        -Wall -Wextra -Wpedantic -Wno-pointer-arith
                        -Wno-sign-compare -UNDEBUG -ffunction-sections)
target_link_options(dm_mc02_ospi_dma_integration_smoke PRIVATE
                     -Wl,--gc-sections)

add_executable(dm_mc02_dma_fifo_dbm_endpoint_smoke
    tests/dma_fifo_dbm_endpoint_smoke.c
    qemu/upstream/hw/arm/dm_mc02_dma.c
    qemu/upstream/hw/arm/dm_mc02_dma_endpoint.c)
target_include_directories(dm_mc02_dma_fifo_dbm_endpoint_smoke PRIVATE
                           qemu/upstream
                           qemu/upstream/include
                           qemu/upstream/hw/arm
                           "${CMAKE_CURRENT_BINARY_DIR}/../qemu")
target_link_libraries(dm_mc02_dma_fifo_dbm_endpoint_smoke PRIVATE
                      PkgConfig::GLIB2 m pthread)
set_property(TARGET dm_mc02_dma_fifo_dbm_endpoint_smoke PROPERTY C_EXTENSIONS ON)
target_compile_options(dm_mc02_dma_fifo_dbm_endpoint_smoke PRIVATE
                        -Wall -Wextra -Wno-pedantic -Wno-unused-parameter
                        -Wno-sign-compare -Wno-pointer-arith -UNDEBUG
                        -ffunction-sections)
target_link_options(dm_mc02_dma_fifo_dbm_endpoint_smoke PRIVATE
                     -Wl,--gc-sections)

include(CTest)
if(BUILD_TESTING)
    add_test(NAME dm_mc02_wire_smoke COMMAND dm_mc02_wire_smoke)
    set_tests_properties(dm_mc02_wire_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_v2_wire_smoke COMMAND dm_mc02_v2_wire_smoke)
    set_tests_properties(dm_mc02_v2_wire_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_v2_wire_vectors
             COMMAND dm_mc02_v2_wire_vectors
                     "${CMAKE_CURRENT_SOURCE_DIR}/tests/v2_wire_vectors.txt")
    set_tests_properties(dm_mc02_v2_wire_vectors PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_v2_payload_smoke COMMAND dm_mc02_v2_payload_smoke)
    set_tests_properties(dm_mc02_v2_payload_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_v2_payload_vectors
             COMMAND dm_mc02_v2_payload_vectors
                     "${CMAKE_CURRENT_SOURCE_DIR}/tests/v2_payload_vectors.txt")
    set_tests_properties(dm_mc02_v2_payload_vectors PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_v2_adc_payload_smoke
             COMMAND dm_mc02_v2_adc_payload_smoke)
    set_tests_properties(dm_mc02_v2_adc_payload_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_cosim_smoke COMMAND dm_mc02_cosim_smoke)
    add_test(NAME dm_mc02_transport_smoke COMMAND dm_mc02_transport_smoke)
    add_test(NAME dm_usb_host_descriptor_smoke
             COMMAND dm_usb_host_descriptor_smoke)
    add_test(NAME dm_usb_host_pipe_smoke COMMAND dm_usb_host_pipe_smoke)
    add_test(NAME dm_usb_host_retry_smoke COMMAND dm_usb_host_retry_smoke)
    add_test(NAME dm_stm32h7_usb_host_pipe_smoke
             COMMAND dm_stm32h7_usb_host_pipe_smoke)
    add_test(NAME dm_stm32h7_usb_host_control_nak_smoke
             COMMAND dm_stm32h7_usb_host_control_nak_smoke)
    add_test(NAME dm_stm32h7_usb_host_pipe_async_smoke
             COMMAND dm_stm32h7_usb_host_pipe_async_smoke)
    add_test(NAME dm_stm32h7_usb_host_pipe_async_dispatch_smoke
             COMMAND dm_stm32h7_usb_host_pipe_async_dispatch_smoke)
    add_test(NAME dm_stm32h7_usb_host_async_dispatch_contract_smoke
             COMMAND dm_stm32h7_usb_host_async_dispatch_contract_smoke)
    add_test(NAME dm_stm32h7_usb_host_bulk_async_smoke
             COMMAND dm_stm32h7_usb_host_bulk_async_smoke)
    add_test(NAME dm_usb_host_endpoint_state_smoke
             COMMAND dm_usb_host_endpoint_state_smoke)
    add_test(NAME dm_usb_host_channel_allocator_smoke
             COMMAND dm_usb_host_channel_allocator_smoke)
    add_test(NAME dm_usb_host_channel_operation_smoke
             COMMAND dm_usb_host_channel_operation_smoke)
    add_test(NAME dm_usb_host_channel_completion_smoke
             COMMAND dm_usb_host_channel_completion_smoke)
    add_test(NAME dm_usb_host_bulk_smoke COMMAND dm_usb_host_bulk_smoke)
    add_test(NAME dm_stm32h7_usb_host_bulk_smoke
             COMMAND dm_stm32h7_usb_host_bulk_smoke)
    add_test(NAME dm_usb_host_periodic_schedule_smoke
             COMMAND dm_usb_host_periodic_schedule_smoke)
    add_test(NAME dm_usb_host_periodic_poller_smoke
             COMMAND dm_usb_host_periodic_poller_smoke)
    add_test(NAME dm_usb_host_sof_clock_smoke
             COMMAND dm_usb_host_sof_clock_smoke)
    add_test(NAME dm_stm32h7_usb_host_periodic_sof_smoke
             COMMAND dm_stm32h7_usb_host_periodic_sof_smoke)
    add_test(NAME dm_stm32h7_usb_host_periodic_smoke
             COMMAND dm_stm32h7_usb_host_periodic_smoke)
    add_test(NAME dm_usb_host_periodic_registry_smoke
             COMMAND dm_usb_host_periodic_registry_smoke)
    add_test(NAME dm_stm32h7_usb_host_periodic_registry_sof_smoke
             COMMAND dm_stm32h7_usb_host_periodic_registry_sof_smoke)
    add_test(NAME dm_stm32h7_usb_host_sof_irq_smoke
             COMMAND dm_stm32h7_usb_host_sof_irq_smoke)
    set_tests_properties(dm_mc02_cosim_smoke dm_mc02_transport_smoke
                         PROPERTIES TIMEOUT 5)
    set_tests_properties(dm_usb_host_descriptor_smoke dm_usb_host_pipe_smoke
                         dm_usb_host_retry_smoke
                         dm_stm32h7_usb_host_pipe_smoke
                         dm_stm32h7_usb_host_pipe_async_smoke
                         dm_stm32h7_usb_host_pipe_async_dispatch_smoke
                         dm_stm32h7_usb_host_async_dispatch_contract_smoke
                         dm_stm32h7_usb_host_bulk_async_smoke
                         dm_usb_host_endpoint_state_smoke
                         dm_usb_host_channel_allocator_smoke
                         dm_usb_host_channel_operation_smoke
                         dm_usb_host_channel_completion_smoke
                         dm_usb_host_bulk_smoke
                         dm_stm32h7_usb_host_bulk_smoke
                         dm_usb_host_periodic_schedule_smoke
                         dm_usb_host_periodic_poller_smoke
                         dm_usb_host_sof_clock_smoke
                         dm_stm32h7_usb_host_periodic_sof_smoke
                         dm_stm32h7_usb_host_periodic_smoke
                         dm_usb_host_periodic_registry_smoke
                         dm_stm32h7_usb_host_periodic_registry_sof_smoke
                         dm_stm32h7_usb_host_sof_irq_smoke PROPERTIES TIMEOUT 5)
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    add_test(NAME dm_mc02_worker_transport
             COMMAND "${Python3_EXECUTABLE}"
                     "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_worker_transport.py")
    set_tests_properties(dm_mc02_worker_transport PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_motor_protocol
             COMMAND "${Python3_EXECUTABLE}"
                     "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_motor_protocol.py")
    set_tests_properties(dm_mc02_motor_protocol PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_bmi088_timing_smoke
             COMMAND dm_mc02_bmi088_timing_smoke)
    set_tests_properties(dm_mc02_bmi088_timing_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_bmi088_fifo_smoke
             COMMAND dm_mc02_bmi088_fifo_smoke)
    set_tests_properties(dm_mc02_bmi088_fifo_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_bmi088_signal_smoke
             COMMAND dm_mc02_bmi088_signal_smoke)
    set_tests_properties(dm_mc02_bmi088_signal_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_bmi088_drift_smoke
             COMMAND dm_mc02_bmi088_drift_smoke)
    set_tests_properties(dm_mc02_bmi088_drift_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_bmi088_chip_smoke
             COMMAND dm_mc02_bmi088_chip_smoke)
    set_tests_properties(dm_mc02_bmi088_chip_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_bmi088_spi_smoke
             COMMAND dm_mc02_bmi088_spi_smoke)
    set_tests_properties(dm_mc02_bmi088_spi_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_spi_target_smoke COMMAND dm_mc02_spi_target_smoke)
    set_tests_properties(dm_mc02_spi_target_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_spi_nor_flash_smoke COMMAND dm_spi_nor_flash_smoke)
    set_tests_properties(dm_spi_nor_flash_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_nor_flash_smoke COMMAND dm_nor_flash_smoke)
    set_tests_properties(dm_nor_flash_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_nor_flash_persistence_smoke
             COMMAND dm_nor_flash_persistence_smoke)
    set_tests_properties(dm_nor_flash_persistence_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_board_profile_smoke
             COMMAND dm_mc02_board_profile_smoke)
    set_tests_properties(dm_mc02_board_profile_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_soc_map_smoke COMMAND dm_mc02_soc_map_smoke)
    set_tests_properties(dm_mc02_soc_map_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_rng_smoke COMMAND dm_mc02_rng_smoke)
    set_tests_properties(dm_mc02_rng_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_trigger_smoke COMMAND dm_mc02_trigger_smoke)
    set_tests_properties(dm_mc02_trigger_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_stm32h7_clock_tree_smoke
             COMMAND dm_stm32h7_clock_tree_smoke)
    set_tests_properties(dm_stm32h7_clock_tree_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_uart_timing_smoke COMMAND dm_uart_timing_smoke)
    set_tests_properties(dm_uart_timing_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_dma_endpoint_smoke COMMAND dm_mc02_dma_endpoint_smoke)
    set_tests_properties(dm_mc02_dma_endpoint_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_ospi_dma_endpoint_smoke
             COMMAND dm_mc02_ospi_dma_endpoint_smoke)
    set_tests_properties(dm_mc02_ospi_dma_endpoint_smoke PROPERTIES TIMEOUT 5)
    add_test(NAME dm_mc02_ospi_dma_integration_smoke
             COMMAND dm_mc02_ospi_dma_integration_smoke)
    set_tests_properties(dm_mc02_ospi_dma_integration_smoke PROPERTIES
                         TIMEOUT 5)
    add_test(NAME dm_mc02_dma_fifo_dbm_endpoint_smoke
             COMMAND dm_mc02_dma_fifo_dbm_endpoint_smoke)
    set_tests_properties(dm_mc02_dma_fifo_dbm_endpoint_smoke PROPERTIES
                         TIMEOUT 5)
    add_test(NAME dm_mc02_qemu_smoke_suite
             COMMAND bash
                     "${CMAKE_CURRENT_SOURCE_DIR}/tools/run-qemu-smoke-suite.sh")
    set_tests_properties(dm_mc02_qemu_smoke_suite
                         PROPERTIES TIMEOUT 900 SKIP_RETURN_CODE 77)
endif()
