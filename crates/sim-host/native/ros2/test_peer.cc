// Test-only independent DDS peer using official message definitions and executor.
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_srvs/srv/empty.hpp>
#include <poll.h>
#include <unistd.h>
#include <chrono>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>

int main(int argc, char **argv) {
    using namespace std::chrono_literals;
    try {
        if (argc != 2) throw std::runtime_error("expected unique ROS topic prefix");
        const std::string prefix(argv[1]);
        auto context = std::make_shared<rclcpp::Context>();
        rclcpp::InitOptions init;
        init.shutdown_on_signal = false;
        context->init(0, nullptr, init);
        rclcpp::NodeOptions options;
        options.context(context).use_global_arguments(false).enable_rosout(false);
        auto node = std::make_shared<rclcpp::Node>("rust_ros2_test_peer", options);
        auto imu = node->create_publisher<sensor_msgs::msg::Imu>(prefix + "/imu", 10);
        auto joints = node->create_publisher<sensor_msgs::msg::JointState>(prefix + "/joints", 10);
        std::vector<double> efforts;
        auto effort_sub = node->create_subscription<std_msgs::msg::Float64MultiArray>(
            prefix + "/motors", 10, [&](std_msgs::msg::Float64MultiArray::ConstSharedPtr msg) {
                efforts = msg->data;
            });
        std::size_t resets = 0;
        auto reset = node->create_service<std_srvs::srv::Empty>(prefix + "/reset",
            [&](std::shared_ptr<std_srvs::srv::Empty::Request>,
                std::shared_ptr<std_srvs::srv::Empty::Response>) { ++resets; });
        rclcpp::ExecutorOptions executor_options;
        executor_options.context = context;
        rclcpp::executors::SingleThreadedExecutor executor(executor_options);
        executor.add_node(node);
        auto deadline = std::chrono::steady_clock::now() + 20s;
        while (imu->get_subscription_count() == 0 || joints->get_subscription_count() == 0
            || effort_sub->get_publisher_count() == 0) {
            if (std::chrono::steady_clock::now() >= deadline)
                throw std::runtime_error("ROS peer discovery timed out");
            executor.spin_once(1ms);
        }
        std::cout << "READY\n" << std::flush;
        bool done = false;
        std::string pending;
        while (!done) {
            if (std::chrono::steady_clock::now() >= deadline)
                throw std::runtime_error("ROS peer command deadline expired");
            executor.spin_once(1ms);
            pollfd input{0, POLLIN, 0};
            if (::poll(&input, 1, 0) > 0) {
                char bytes[256];
                const auto count = ::read(0, bytes, sizeof(bytes));
                if (count <= 0) break;
                pending.append(bytes, static_cast<std::size_t>(count));
                if (pending.size() > 4096) throw std::runtime_error("ROS peer input too long");
                for (auto end = pending.find('\n'); end != std::string::npos; end = pending.find('\n')) {
                    const auto line = pending.substr(0, end);
                    pending.erase(0, end + 1);
                    std::istringstream command(line);
                    std::string kind;
                    command >> kind;
                    if (kind == "quit") { done = true; break; }
                    if (kind == "sample" || kind == "badstamp" || kind == "nan") {
                        int stamp = 1;
                        command >> stamp;
                        sensor_msgs::msg::Imu msg;
                        msg.header.stamp.sec = kind == "badstamp" ? -1 : stamp;
                        msg.header.stamp.nanosec = 123;
                        msg.angular_velocity.x = kind == "nan" ? std::numeric_limits<double>::quiet_NaN() : stamp;
                        msg.angular_velocity.y = -2; msg.angular_velocity.z = 3;
                        msg.linear_acceleration.x = 4;
                        msg.linear_acceleration.y = -5; msg.linear_acceleration.z = 9.80665;
                        imu->publish(msg);
                        if (kind == "sample") {
                            sensor_msgs::msg::JointState state;
                            state.name = {"names", "are", "ignored"};
                            state.position = {0.1, 0.2, 999};
                            state.velocity = {1.1, 1.2}; state.effort = {2.1};
                            joints->publish(state);
                        }
                        std::cout << "SENT\n" << std::flush;
                    } else if (kind == "short") {
                        sensor_msgs::msg::JointState state;
                        state.position = {0.7};
                        joints->publish(state);
                        std::cout << "SENT\n" << std::flush;
                    } else if (kind == "status") {
                        std::cout << "STATUS " << resets;
                        for (double effort : efforts) std::cout << ' ' << effort;
                        std::cout << '\n' << std::flush;
                    } else throw std::runtime_error("unknown ROS peer command");
                }
            }
        }
        executor.remove_node(node);
        reset.reset(); effort_sub.reset(); joints.reset(); imu.reset(); node.reset();
        if (!context->shutdown("ROS peer finished")) throw std::runtime_error("peer shutdown failed");
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "ROS peer: " << error.what() << '\n';
        return 1;
    }
}
