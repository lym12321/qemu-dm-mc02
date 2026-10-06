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
option(DM_GAZEBO_RENDERING "Enable native headless camera and GPU lidar snapshots" OFF)
if(DM_GAZEBO_RENDERING)
    find_package(gz-rendering8 8.2.3 EXACT REQUIRED COMPONENTS ogre2)
    find_package(gz-transport13 13.6.0 EXACT REQUIRED)
    find_package(gz-msgs10 10.4.0 EXACT REQUIRED)
endif()

pybind11_add_module(_dm_gazebo_native MODULE world.cc)
target_compile_features(_dm_gazebo_native PRIVATE cxx_std_17)
target_compile_options(_dm_gazebo_native PRIVATE -Wall -Wextra -Werror)
target_link_libraries(_dm_gazebo_native PRIVATE gz-sim8::gz-sim8
    gz-sensors8::gz-sensors8-imu)
if(DM_GAZEBO_RENDERING)
    target_compile_definitions(_dm_gazebo_native PRIVATE DM_GAZEBO_RENDERING)
    target_link_libraries(_dm_gazebo_native PRIVATE
        gz-transport13::gz-transport13 gz-msgs10::gz-msgs10)
endif()

include(CTest)
option(DM_GAZEBO_RENDERING_TESTS "Test native headless Ogre2 camera and GPU lidar" OFF)
option(DM_GAZEBO_ROS_TESTS "Test rendered messages through the official ROS bridge" OFF)
if(DM_GAZEBO_ROS_TESTS AND NOT DM_GAZEBO_RENDERING_TESTS)
    message(FATAL_ERROR "DM_GAZEBO_ROS_TESTS requires DM_GAZEBO_RENDERING_TESTS")
endif()
if(DM_GAZEBO_RENDERING_TESTS AND NOT BUILD_TESTING)
    message(FATAL_ERROR "DM_GAZEBO_RENDERING_TESTS requires BUILD_TESTING")
endif()
if(BUILD_TESTING)
    if(DM_GAZEBO_RENDERING)
        set(render_admission "admission")
    else()
        set(render_admission "compile_off or admission_python")
    endif()
    add_test(NAME gazebo-native-rendering-admission COMMAND "${Python3_EXECUTABLE}"
        -m pytest "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_rendering.py" -q
        -k "${render_admission}" --junitxml=${CMAKE_CURRENT_BINARY_DIR}/render-admission-junit.xml)
    set_tests_properties(gazebo-native-rendering-admission PROPERTIES TIMEOUT 15
        ENVIRONMENT "PYTHONPATH=$<TARGET_FILE_DIR:_dm_gazebo_native>;PYTEST_DISABLE_PLUGIN_AUTOLOAD=1")
    if(DM_GAZEBO_RENDERING)
        add_executable(gazebo-rendering-mailbox-smoke tests/rendering_mailbox_smoke.cc)
        target_compile_features(gazebo-rendering-mailbox-smoke PRIVATE cxx_std_17)
        target_compile_options(gazebo-rendering-mailbox-smoke PRIVATE -Wall -Wextra -Werror)
        target_link_libraries(gazebo-rendering-mailbox-smoke PRIVATE
            gz-transport13::gz-transport13 gz-msgs10::gz-msgs10)
        add_test(NAME gazebo-native-rendering-mailbox COMMAND gazebo-rendering-mailbox-smoke)
        set_tests_properties(gazebo-native-rendering-mailbox PROPERTIES TIMEOUT 20)
        add_test(NAME gazebo-native-rendering-consumer COMMAND "${Python3_EXECUTABLE}"
            -m pytest "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_rendering.py" -q -s
            -k consumer --junitxml=${CMAKE_CURRENT_BINARY_DIR}/render-consumer-junit.xml)
        set_tests_properties(gazebo-native-rendering-consumer PROPERTIES TIMEOUT 45
            ENVIRONMENT "PYTHONPATH=$<TARGET_FILE_DIR:_dm_gazebo_native>;PYTEST_DISABLE_PLUGIN_AUTOLOAD=1")
    endif()
    add_executable(gazebo-resources-smoke tests/resources_smoke.cc)
    target_compile_features(gazebo-resources-smoke PRIVATE cxx_std_17)
    target_compile_options(gazebo-resources-smoke PRIVATE -Wall -Wextra -Werror)
    target_link_libraries(gazebo-resources-smoke PRIVATE gz-sim8::gz-sim8)
    add_test(NAME gazebo-native-resources COMMAND gazebo-resources-smoke
        "${CMAKE_CURRENT_SOURCE_DIR}/../../../models/gazebo_rmua19.sdf"
        "${CMAKE_CURRENT_SOURCE_DIR}/../../../models/vendor/rmua19/resource/models")
    set_tests_properties(gazebo-native-resources PROPERTIES TIMEOUT 15)
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
        if(DM_GAZEBO_ROS_TESTS)
            find_package(ros_gz_bridge 1.0.24 EXACT REQUIRED)
            find_package(rclcpp REQUIRED)
            find_package(sensor_msgs REQUIRED)
            find_program(DM_GAZEBO_BRIDGE_NODE bridge_node
                HINTS "${ros_gz_bridge_DIR}/../../../lib/ros_gz_bridge" NO_DEFAULT_PATH)
            if(NOT DM_GAZEBO_BRIDGE_NODE)
                message(FATAL_ERROR "The official ros_gz_bridge bridge_node is required")
            endif()
            add_executable(gazebo-ros-rendering-smoke tests/rendering_smoke.cc)
            target_compile_definitions(gazebo-ros-rendering-smoke PRIVATE DM_GAZEBO_WITH_ROS)
            target_compile_features(gazebo-ros-rendering-smoke PRIVATE cxx_std_17)
            target_compile_options(gazebo-ros-rendering-smoke PRIVATE -Wall -Wextra -Werror)
            target_link_libraries(gazebo-ros-rendering-smoke PRIVATE gz-sim8::gz-sim8
                gz-transport13::gz-transport13 gz-msgs10::gz-msgs10 rclcpp::rclcpp
                sensor_msgs::sensor_msgs__rosidl_typesupport_cpp)
            add_test(NAME gazebo-native-ros COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/ros_smoke.py"
                "${DM_GAZEBO_BRIDGE_NODE}" "$<TARGET_FILE:gazebo-ros-rendering-smoke>"
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/rendering.sdf"
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/ros_bridge.yaml")
            set_tests_properties(gazebo-native-ros PROPERTIES TIMEOUT 30)
            if(DM_GAZEBO_RENDERING)
                add_test(NAME gazebo-native-ros-consumer COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/ros_smoke.py"
                    "${DM_GAZEBO_BRIDGE_NODE}" "$<TARGET_FILE:gazebo-ros-rendering-smoke>"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/rendering.sdf"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/ros_bridge.yaml"
                    --production "$<TARGET_FILE:_dm_gazebo_native>")
                set_tests_properties(gazebo-native-ros-consumer PROPERTIES TIMEOUT 30)
            endif()
        endif()
    endif()
endif()
