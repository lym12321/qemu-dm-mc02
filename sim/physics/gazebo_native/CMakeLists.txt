cmake_minimum_required(VERSION 3.16)
project(dm_gazebo_native_tests LANGUAGES CXX)

# Production world.cc is built once through Cargo/CXX. This entry only owns
# independent native resource, mailbox and optional rendering/ROS test binaries.
find_package(gz-sim8 8.15.0 EXACT REQUIRED)
option(DM_GAZEBO_RENDERING "Test the native rendering mailbox" OFF)
option(DM_GAZEBO_RENDERING_TESTS "Test native headless Ogre2 camera and GPU lidar" OFF)
option(DM_GAZEBO_ROS_TESTS "Test rendered messages through the official ROS bridge" OFF)
set(DM_GAZEBO_RUST_PRODUCER "" CACHE FILEPATH "Rust gazebo_render_producer example executable")
if(DM_GAZEBO_ROS_TESTS AND NOT DM_GAZEBO_RENDERING_TESTS)
    message(FATAL_ERROR "DM_GAZEBO_ROS_TESTS requires DM_GAZEBO_RENDERING_TESTS")
endif()
include(CTest)
if(DM_GAZEBO_RENDERING_TESTS AND NOT BUILD_TESTING)
    message(FATAL_ERROR "DM_GAZEBO_RENDERING_TESTS requires BUILD_TESTING")
endif()
if(DM_GAZEBO_RENDERING OR DM_GAZEBO_RENDERING_TESTS)
    find_package(gz-transport13 13.6.0 EXACT REQUIRED)
    find_package(gz-msgs10 10.4.0 EXACT REQUIRED)
endif()
if(BUILD_TESTING)
    if(DM_GAZEBO_RENDERING)
        add_executable(gazebo-rendering-mailbox-smoke tests/rendering_mailbox_smoke.cc)
        target_compile_features(gazebo-rendering-mailbox-smoke PRIVATE cxx_std_17)
        target_compile_options(gazebo-rendering-mailbox-smoke PRIVATE -Wall -Wextra -Werror)
        target_link_libraries(gazebo-rendering-mailbox-smoke PRIVATE
            gz-transport13::gz-transport13 gz-msgs10::gz-msgs10)
        add_test(NAME gazebo-native-rendering-mailbox COMMAND gazebo-rendering-mailbox-smoke)
        set_tests_properties(gazebo-native-rendering-mailbox PROPERTIES TIMEOUT 20)
    endif()
    add_executable(gazebo-resources-smoke tests/resources_smoke.cc)
    target_compile_features(gazebo-resources-smoke PRIVATE cxx_std_17)
    target_compile_options(gazebo-resources-smoke PRIVATE -Wall -Wextra -Werror)
    target_link_libraries(gazebo-resources-smoke PRIVATE gz-sim8::gz-sim8)
    add_test(NAME gazebo-native-resources COMMAND gazebo-resources-smoke
        "${CMAKE_CURRENT_SOURCE_DIR}/../../../models/gazebo_rmua19.sdf"
        "${CMAKE_CURRENT_SOURCE_DIR}/../../../models/vendor/rmua19/resource/models")
    set_tests_properties(gazebo-native-resources PROPERTIES TIMEOUT 15)
    if(DM_GAZEBO_RENDERING_TESTS)
        find_package(gz-rendering8 8.2.3 EXACT REQUIRED COMPONENTS ogre2)
        add_executable(gazebo-rendering-smoke tests/rendering_smoke.cc)
        target_compile_features(gazebo-rendering-smoke PRIVATE cxx_std_17)
        target_compile_options(gazebo-rendering-smoke PRIVATE -Wall -Wextra -Werror)
        target_link_libraries(gazebo-rendering-smoke PRIVATE gz-sim8::gz-sim8
            gz-transport13::gz-transport13 gz-msgs10::gz-msgs10)
        add_test(NAME gazebo-native-rendering COMMAND gazebo-rendering-smoke
            "${CMAKE_CURRENT_SOURCE_DIR}/tests/rendering.sdf")
        set_tests_properties(gazebo-native-rendering PROPERTIES TIMEOUT 20)
        if(DM_GAZEBO_ROS_TESTS)
            find_package(Python3 REQUIRED COMPONENTS Interpreter)
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
            if(DM_GAZEBO_RUST_PRODUCER)
                add_test(NAME gazebo-native-ros-consumer COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/ros_smoke.py"
                    "${DM_GAZEBO_BRIDGE_NODE}" "$<TARGET_FILE:gazebo-ros-rendering-smoke>"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/rendering.sdf"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/ros_bridge.yaml"
                    --rust-producer "${DM_GAZEBO_RUST_PRODUCER}")
                set_tests_properties(gazebo-native-ros-consumer PROPERTIES TIMEOUT 30)
            endif()
        endif()
    endif()
endif()
