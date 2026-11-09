#include "sim-host/src/ros2/ffi.rs.h"
#include "ros_transport.hh"
#include <rclcpp/rclcpp.hpp>
#include <rclcpp/executors/single_threaded_executor.hpp>
#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_srvs/srv/empty.hpp>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <exception>
#include <future>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace dm_ros2 {
namespace {
void require(bool value, const char *message) {
    if (!value) throw std::runtime_error(message);
}
template<class Values> void finite(const Values &values) {
    require(std::all_of(values.begin(), values.end(), [](double v) { return std::isfinite(v); }),
        "nonfinite ROS2 sensor value");
}
}

struct Transport::Impl {
    using Imu = sensor_msgs::msg::Imu;
    using Joints = sensor_msgs::msg::JointState;
    using Efforts = std_msgs::msg::Float64MultiArray;
    using Empty = std_srvs::srv::Empty;
    std::size_t motor_count;
    rclcpp::Context::SharedPtr context;
    rclcpp::Node::SharedPtr node;
    std::unique_ptr<rclcpp::executors::SingleThreadedExecutor> executor;
    rclcpp::Publisher<Efforts>::SharedPtr publisher;
    rclcpp::Subscription<Imu>::SharedPtr imu_subscription;
    rclcpp::Subscription<Joints>::SharedPtr joint_subscription;
    rclcpp::Client<Empty>::SharedPtr reset_client;
    Snapshot snapshot;
    std::exception_ptr callback_failure;
    bool added = false, closed = false;

    explicit Impl(std::size_t count) : motor_count(count) { clear(); }

    void clear() {
        snapshot = Snapshot{};
        snapshot.accel_m_s2 = {0, 0, 9.80665};
        for (std::size_t i = 0; i < motor_count; ++i) {
            snapshot.position_rad.push_back(0);
            snapshot.velocity_rad_s.push_back(0);
            snapshot.effort_nm.push_back(0);
            snapshot.effort_valid.push_back(0);
        }
        callback_failure = nullptr;
    }
    void check() const {
        require(!closed, "ROS2 transport is closed");
        if (callback_failure) std::rethrow_exception(callback_failure);
        require(context && context->is_valid(), "ROS2 context stopped");
    }
    void imu(const Imu &msg) noexcept {
        if (callback_failure) return;
        try {
            const std::array<double, 3> gyro{msg.angular_velocity.x,
                msg.angular_velocity.y, msg.angular_velocity.z};
            const std::array<double, 3> accel{msg.linear_acceleration.x,
                msg.linear_acceleration.y, msg.linear_acceleration.z};
            finite(gyro); finite(accel);
            snapshot.gyro_rad_s = gyro;
            snapshot.accel_m_s2 = accel;
            if (msg.header.stamp.sec >= 0 && msg.header.stamp.nanosec < 1000000000) {
                snapshot.source_time_ns = static_cast<std::uint64_t>(msg.header.stamp.sec)
                    * 1000000000 + msg.header.stamp.nanosec;
                snapshot.source_time_valid = true;
            }
        } catch (...) { callback_failure = std::current_exception(); }
    }
    void joints(const Joints &msg) noexcept {
        if (callback_failure) return;
        try {
            // Admission only examines consumed indexed fields; names are not a binding.
            auto positions = snapshot.position_rad;
            auto velocities = snapshot.velocity_rad_s;
            auto efforts = snapshot.effort_nm;
            auto valid = snapshot.effort_valid;
            for (std::size_t i = 0; i < motor_count; ++i) {
                positions[i] = i < msg.position.size() ? msg.position[i] : 0;
                velocities[i] = i < msg.velocity.size() ? msg.velocity[i] : 0;
                efforts[i] = i < msg.effort.size() ? msg.effort[i] : 0;
                valid[i] = i < msg.effort.size();
            }
            finite(positions); finite(velocities); finite(efforts);
            snapshot.position_rad = std::move(positions);
            snapshot.velocity_rad_s = std::move(velocities);
            snapshot.effort_nm = std::move(efforts);
            snapshot.effort_valid = std::move(valid);
        } catch (...) { callback_failure = std::current_exception(); }
    }
    void publish(const std::vector<double> &values) {
        Efforts msg;
        msg.data = values;
        publisher->publish(msg);
    }
    void close() {
        if (closed) return;
        closed = true;
        std::exception_ptr failure;
        auto attempt = [&failure](auto action) {
            try { action(); } catch (...) { if (!failure) failure = std::current_exception(); }
        };
        if (executor) attempt([&] { executor->cancel(); });
        if (added) attempt([&] { executor->remove_node(node); });
        added = false;
        reset_client.reset(); joint_subscription.reset(); imu_subscription.reset();
        publisher.reset(); executor.reset(); node.reset();
        if (context && context->is_valid()) attempt([&] {
            require(context->shutdown("Rust ROS2 transport closed"), "ROS2 context shutdown failed");
        });
        context.reset();
        if (failure) std::rethrow_exception(failure);
    }
};

Transport::Transport(std::size_t motor_count, rust::Str motor_topic,
    rust::Str imu_topic, rust::Str joint_state_topic, rust::Str node_name,
    rust::Str reset_service) : impl_(std::make_unique<Impl>(motor_count)) {
    try {
        auto &p = *impl_;
        p.context = std::make_shared<rclcpp::Context>();
        rclcpp::InitOptions init;
        init.shutdown_on_signal = false;
        p.context->init(0, nullptr, init);
        rclcpp::NodeOptions options;
        options.context(p.context).use_global_arguments(false).enable_rosout(false);
        p.node = std::make_shared<rclcpp::Node>(std::string(node_name), options);
        rclcpp::ExecutorOptions executor_options;
        executor_options.context = p.context;
        p.executor = std::make_unique<rclcpp::executors::SingleThreadedExecutor>(executor_options);
        const auto qos = rclcpp::QoS(rclcpp::KeepLast(10));
        p.publisher = p.node->create_publisher<Impl::Efforts>(std::string(motor_topic), qos);
        p.imu_subscription = p.node->create_subscription<Impl::Imu>(std::string(imu_topic), qos,
            [&p](Impl::Imu::ConstSharedPtr msg) { p.imu(*msg); });
        p.joint_subscription = p.node->create_subscription<Impl::Joints>(std::string(joint_state_topic), qos,
            [&p](Impl::Joints::ConstSharedPtr msg) { p.joints(*msg); });
        if (!reset_service.empty())
            p.reset_client = p.node->create_client<Impl::Empty>(std::string(reset_service));
        p.executor->add_node(p.node);
        p.added = true;
    } catch (...) {
        const auto primary = std::current_exception();
        try { impl_->close(); }
        catch (const std::exception &cleanup) {
            try { std::cerr << "ROS2 construction cleanup failed: " << cleanup.what() << '\n'; }
            catch (...) {}
        }
        catch (...) {
            try { std::cerr << "ROS2 construction cleanup failed with a nonstandard exception\n"; }
            catch (...) {}
        }
        std::rethrow_exception(primary);
    }
}

Transport::~Transport() noexcept { try { close(); } catch (...) {} }
Snapshot Transport::sample() const { impl_->check(); return impl_->snapshot; }
void Transport::publish_efforts(rust::Slice<const double> values) {
    impl_->check();
    require(values.size() == impl_->motor_count, "ROS2 effort array size mismatch");
    finite(values);
    impl_->publish(std::vector<double>(values.begin(), values.end()));
}
void Transport::pump(std::uint64_t timeout_ns) {
    impl_->check();
    impl_->executor->spin_once(std::chrono::nanoseconds(timeout_ns));
    impl_->check();
}
void Transport::reset(std::uint64_t timeout_ns) {
    auto &p = *impl_;
    require(!p.closed, "ROS2 transport is closed");
    p.clear();
    p.check();
    p.publish(std::vector<double>(p.motor_count, 0));
    if (!p.reset_client) return;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::nanoseconds(timeout_ns);
    auto future = p.reset_client->async_send_request(std::make_shared<Impl::Empty::Request>());
    try {
        while (future.wait_for(std::chrono::seconds(0)) != std::future_status::ready) {
            p.check();
            const auto remaining = std::chrono::duration_cast<std::chrono::nanoseconds>(
                deadline - std::chrono::steady_clock::now());
            require(remaining.count() > 0, "ROS2 reset service timed out");
            p.executor->spin_once(std::min(remaining, std::chrono::nanoseconds(10000000)));
        }
        require(static_cast<bool>(future.get()), "ROS2 reset service failed");
        p.check();
    } catch (...) {
        p.reset_client->remove_pending_request(future);
        throw;
    }
}
void Transport::close() { impl_->close(); }
std::unique_ptr<Transport> new_transport(std::size_t motor_count,
    rust::Str motor_topic, rust::Str imu_topic, rust::Str joint_state_topic,
    rust::Str node_name, rust::Str reset_service) {
    return std::make_unique<Transport>(motor_count, motor_topic, imu_topic,
        joint_state_topic, node_name, reset_service);
}
}
