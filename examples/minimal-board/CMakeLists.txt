cmake_minimum_required(VERSION 3.20)
if(NOT DEFINED CMAKE_TOOLCHAIN_FILE)
    set(CMAKE_TOOLCHAIN_FILE "${CMAKE_CURRENT_LIST_DIR}/arm-none-eabi.cmake"
        CACHE FILEPATH "ARM bare-metal toolchain")
endif()
project(sim_minimal_board LANGUAGES C)
include("${CMAKE_CURRENT_LIST_DIR}/firmware.cmake")
