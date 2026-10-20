# Shared only by the two bare-metal Simulation examples.
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(common "${CMAKE_CURRENT_LIST_DIR}")
add_executable(firmware "${CMAKE_CURRENT_SOURCE_DIR}/main.c" "${common}/startup.c")
set_target_properties(firmware PROPERTIES SUFFIX ".elf"
    LINK_DEPENDS "${common}/firmware.ld")
target_include_directories(firmware PRIVATE "${common}")
target_compile_options(firmware PRIVATE -mcpu=cortex-m7 -mthumb
    -ffreestanding -fno-builtin -fno-stack-protector
    -ffunction-sections -fdata-sections -Wall -Wextra -Werror
    $<$<CONFIG:Debug>:-Og;-g3> $<$<CONFIG:Release>:-O2;-g>)
target_link_options(firmware PRIVATE -mcpu=cortex-m7 -mthumb -nostdlib
    -nostartfiles -Wl,--gc-sections -Wl,--build-id=none
    "-T${common}/firmware.ld" "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/firmware.map")
add_custom_command(TARGET firmware POST_BUILD
    COMMAND "${CMAKE_SIZE}" "$<TARGET_FILE:firmware>" VERBATIM)
