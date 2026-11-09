cmake_minimum_required(VERSION 3.22)
project(dm_ros2_native LANGUAGES CXX)
find_package(rclcpp 28.1.22 EXACT REQUIRED)
find_package(sensor_msgs 5.3.8 EXACT REQUIRED)
find_package(std_msgs 5.3.8 EXACT REQUIRED)
find_package(std_srvs 5.3.8 EXACT REQUIRED)
find_package(rmw_fastrtps_cpp 8.4.4 EXACT REQUIRED)

add_library(dm_ros2_transport STATIC ros_transport.cc)
target_compile_features(dm_ros2_transport PUBLIC cxx_std_17)
target_compile_options(dm_ros2_transport PRIVATE -Wall -Wextra -Werror)
target_include_directories(dm_ros2_transport PRIVATE "${CXX_INCLUDE}" "${CMAKE_CURRENT_SOURCE_DIR}")
target_link_libraries(dm_ros2_transport PUBLIC rclcpp::rclcpp
    sensor_msgs::sensor_msgs__rosidl_typesupport_cpp
    std_msgs::std_msgs__rosidl_typesupport_cpp std_srvs::std_srvs__rosidl_typesupport_cpp)
set_target_properties(dm_ros2_transport PROPERTIES POSITION_INDEPENDENT_CODE ON)

# CMake resolves official exported targets and their complete dynamic closure.
# Cargo links the static adapter, using this generated native linker command.
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/link_probe.cc" "int main() { return 0; }\n")
add_executable(dm_ros2_link_probe EXCLUDE_FROM_ALL "${CMAKE_CURRENT_BINARY_DIR}/link_probe.cc")
target_link_libraries(dm_ros2_link_probe PRIVATE dm_ros2_transport)

add_executable(dm_ros2_test_peer EXCLUDE_FROM_ALL test_peer.cc)
target_compile_features(dm_ros2_test_peer PRIVATE cxx_std_17)
target_compile_options(dm_ros2_test_peer PRIVATE -Wall -Wextra -Werror)
target_link_libraries(dm_ros2_test_peer PRIVATE rclcpp::rclcpp
    sensor_msgs::sensor_msgs__rosidl_typesupport_cpp
    std_msgs::std_msgs__rosidl_typesupport_cpp std_srvs::std_srvs__rosidl_typesupport_cpp)
