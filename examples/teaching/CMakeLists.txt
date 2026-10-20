cmake_minimum_required(VERSION 3.20)

if(NOT DEFINED CMAKE_TOOLCHAIN_FILE)
    set(CMAKE_TOOLCHAIN_FILE "${CMAKE_CURRENT_LIST_DIR}/cmake/arm-none-eabi.cmake"
        CACHE FILEPATH "ARM bare-metal toolchain")
endif()

project(dm_mc02_teaching LANGUAGES C ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug CACHE STRING "Debug or Release" FORCE)
endif()
set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS Debug Release)
if(NOT CMAKE_BUILD_TYPE MATCHES "^(Debug|Release)$")
    message(FATAL_ERROR "The teaching example supports Debug and Release builds")
endif()

set(stm32h7 "${CMAKE_CURRENT_SOURCE_DIR}/../../third_party/stm32h7")
set(hal "${stm32h7}/HAL")
set(device "${stm32h7}/CMSIS/Device")
set(linker_script "${stm32h7}/reference/STM32H723ZGTX_FLASH.ld")

add_executable(gpio_key
    gpio_key.c
    "${device}/Source/Templates/gcc/startup_stm32h723xx.s"
    "${device}/Source/Templates/system_stm32h7xx.c"
    "${hal}/Src/stm32h7xx_hal.c"
    "${hal}/Src/stm32h7xx_hal_cortex.c"
    "${hal}/Src/stm32h7xx_hal_rcc.c"
    "${hal}/Src/stm32h7xx_hal_rcc_ex.c"
    "${hal}/Src/stm32h7xx_hal_flash.c"
    "${hal}/Src/stm32h7xx_hal_flash_ex.c"
    "${hal}/Src/stm32h7xx_hal_pwr.c"
    "${hal}/Src/stm32h7xx_hal_pwr_ex.c"
    "${hal}/Src/stm32h7xx_hal_gpio.c"
)
set_target_properties(gpio_key PROPERTIES SUFFIX ".elf" LINK_DEPENDS "${linker_script}")
target_compile_definitions(gpio_key PRIVATE STM32H723xx USE_HAL_DRIVER USE_PWR_LDO_SUPPLY)
target_include_directories(gpio_key PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${hal}/Inc"
    "${device}/Include"
    "${stm32h7}/CMSIS/Core/Include"
)
target_compile_options(gpio_key PRIVATE
    -mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard
    -ffunction-sections -fdata-sections
    $<$<COMPILE_LANGUAGE:C>:-Wall;-Wextra>
    $<$<CONFIG:Debug>:-Og;-g3>
    $<$<CONFIG:Release>:-O2;-g>
)
target_link_options(gpio_key PRIVATE
    -mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard
    --specs=nano.specs --specs=nosys.specs
    "-T${linker_script}"
    -Wl,--gc-sections
    "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/gpio_key.map"
    -Wl,--print-memory-usage
)
add_custom_command(TARGET gpio_key POST_BUILD
    COMMAND "${CMAKE_SIZE}" "$<TARGET_FILE:gpio_key>"
    VERBATIM
)
