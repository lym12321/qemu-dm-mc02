cmake_minimum_required(VERSION 3.16)
project(dm_gazebo_native LANGUAGES CXX)

# This optional SDK build is independent of the QEMU and host-C builds.
find_package(Python3 3.12 REQUIRED COMPONENTS Interpreter Development.Module)
if(NOT Python3_VERSION VERSION_LESS 3.13)
    message(FATAL_ERROR "The fixed Gazebo SDK profile requires CPython 3.12")
endif()
set(PYBIND11_FINDPYTHON ON)
find_package(pybind11 2.11.1 EXACT REQUIRED)
find_package(gz-sim8 8.15.0 EXACT REQUIRED)
find_package(gz-sensors8 8.2.2 EXACT REQUIRED COMPONENTS imu)

pybind11_add_module(_dm_gazebo_native MODULE world.cc)
target_compile_features(_dm_gazebo_native PRIVATE cxx_std_17)
target_compile_options(_dm_gazebo_native PRIVATE -Wall -Wextra -Werror)
target_link_libraries(_dm_gazebo_native PRIVATE gz-sim8::gz-sim8
    gz-sensors8::gz-sensors8-imu)

include(CTest)
option(DM_GAZEBO_RENDERING_TESTS "Test native headless Ogre2 camera and GPU lidar" OFF)
if(DM_GAZEBO_RENDERING_TESTS AND NOT BUILD_TESTING)
    message(FATAL_ERROR "DM_GAZEBO_RENDERING_TESTS requires BUILD_TESTING")
endif()
if(BUILD_TESTING)
    add_test(NAME gazebo-native-joints COMMAND "${Python3_EXECUTABLE}"
        -m pytest "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_world.py" -q
        --junitxml=${CMAKE_CURRENT_BINARY_DIR}/gazebo-junit.xml)
    set_tests_properties(gazebo-native-joints PROPERTIES TIMEOUT 45
        ENVIRONMENT "PYTHONPATH=$<TARGET_FILE_DIR:_dm_gazebo_native>;PYTEST_DISABLE_PLUGIN_AUTOLOAD=1")
    if(DM_GAZEBO_RENDERING_TESTS)
        find_package(gz-rendering8 8.2.3 EXACT REQUIRED COMPONENTS ogre2)
        find_package(gz-transport13 13.6.0 EXACT REQUIRED)
        find_package(gz-msgs10 10.4.0 EXACT REQUIRED)
        add_executable(gazebo-rendering-smoke tests/rendering_smoke.cc)
        target_compile_features(gazebo-rendering-smoke PRIVATE cxx_std_17)
        target_compile_options(gazebo-rendering-smoke PRIVATE -Wall -Wextra -Werror)
        target_link_libraries(gazebo-rendering-smoke PRIVATE gz-sim8::gz-sim8
            gz-transport13::gz-transport13 gz-msgs10::gz-msgs10)
        add_test(NAME gazebo-native-rendering COMMAND gazebo-rendering-smoke
            "${CMAKE_CURRENT_SOURCE_DIR}/tests/rendering.sdf")
        set_tests_properties(gazebo-native-rendering PROPERTIES TIMEOUT 20)
    endif()
endif()
