// Test-only consumer of the external, unmodified ros_gz_bridge process.
#pragma once

#include <gz/msgs/image.pb.h>
#include <gz/msgs/laserscan.pb.h>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp/executors/single_threaded_executor.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <utility>

class RosObserver
{
public:
    using Deadline = std::chrono::steady_clock::time_point;

    RosObserver()
    {
        try
        {
            context_ = std::make_shared<rclcpp::Context>();
            rclcpp::InitOptions init;
            init.shutdown_on_signal = false;
            context_->init(0, nullptr, init);
            rclcpp::NodeOptions options;
            options.context(context_).use_global_arguments(false).enable_rosout(false);
            const char *prefix = std::getenv("DM_GAZEBO_ROS_TOPIC_PREFIX");
            require(prefix && std::string(prefix).size() < 128 && prefix[0] == '/',
                    "ROS gate requires the owner's unique topic prefix");
            options.arguments({"--ros-args", "-r",
                "/dm_render/ros_image:=" + std::string(prefix) + "/ros_image", "-r",
                "/dm_render/ros_scan:=" + std::string(prefix) + "/ros_scan"});
            node_ = std::make_shared<rclcpp::Node>("dm_render_observer", options);
            rclcpp::ExecutorOptions executorOptions;
            executorOptions.context = context_;
            executor_ = std::make_unique<rclcpp::executors::SingleThreadedExecutor>(
                executorOptions);
            auto qos = rclcpp::SensorDataQoS().keep_last(5);
            imageSubscription_ = node_->create_subscription<Image>(
                "/dm_render/ros_image", qos,
                [this](Image::ConstSharedPtr msg) {
                    capture_ros(images_, std::move(msg));
                });
            scanSubscription_ = node_->create_subscription<Scan>(
                "/dm_render/ros_scan", qos,
                [this](Scan::ConstSharedPtr msg) {
                    capture_ros(scans_, std::move(msg));
                });
            executor_->add_node(node_);
            added_ = true;
        }
        catch (...)
        {
            const auto primary = std::current_exception();
            try { close(); }
            catch (const std::exception &failure)
            {
                try { std::cerr << "ROS observer setup cleanup failed: "
                                << failure.what() << '\n'; }
                catch (...) {}
            }
            catch (...) {}
            std::rethrow_exception(primary);
        }
    }

    ~RosObserver() noexcept
    {
        try { close(); }
        catch (...) {}
    }

    RosObserver(const RosObserver &) = delete;
    RosObserver &operator=(const RosObserver &) = delete;

    void wait_ready(Deadline deadline)
    {
        for (;;)
        {
            check(deadline);
            if (imageSubscription_->get_publisher_count() > 0 &&
                scanSubscription_->get_publisher_count() > 0)
            {
                check(deadline);
                return;
            }
            spin(deadline);
        }
    }

    void native_image(const gz::msgs::Image &msg) noexcept
    {
        capture_native(images_, msg);
    }

    void native_scan(const gz::msgs::LaserScan &msg) noexcept
    {
        capture_native(scans_, msg);
    }

    // One bounded executor turn lets the caller also inspect native failures.
    bool pump(Deadline deadline)
    {
        check(deadline);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            check_locked(deadline);
            pair(images_);
            pair(scans_);
            if (complete())
            {
                check_locked(deadline);
                return true;
            }
        }
        spin(deadline);
        check(deadline);
        std::lock_guard<std::mutex> lock(mutex_);
        check_locked(deadline);
        pair(images_);
        pair(scans_);
        check_locked(deadline);
        return complete();
    }

    void report(std::ostream &out)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        out << "ROS image native=" << images_.native.size()
            << " received=" << images_.ros.size() << " paired=" << images_.count
            << " stamps_ns=" << images_.paired[0] << ',' << images_.paired[1]
            << "; scan native=" << scans_.native.size()
            << " received=" << scans_.ros.size() << " paired=" << scans_.count
            << " stamps_ns=" << scans_.paired[0] << ',' << scans_.paired[1];
    }

    // The owner thread calls wait_ready/pump/close; native callbacks may overlap.
    void close()
    {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (closed_)
                return;
            closed_ = true;
        }
        std::exception_ptr failure;
        auto attempt = [&failure](auto action) {
            try { action(); }
            catch (...) { if (!failure) failure = std::current_exception(); }
        };
        if (executor_)
            attempt([&] { executor_->cancel(); });
        if (added_)
            attempt([&] { executor_->remove_node(node_); });
        added_ = false;
        imageSubscription_.reset();
        scanSubscription_.reset();
        executor_.reset();
        node_.reset();
        if (context_ && context_->is_valid())
            attempt([&] {
                require(context_->shutdown("rendering observer closed"),
                        "ROS observer context shutdown failed");
            });
        context_.reset();
        if (failure)
            std::rethrow_exception(failure);
    }

private:
    using Image = sensor_msgs::msg::Image;
    using Scan = sensor_msgs::msg::LaserScan;

    template<class Native, class Ros>
    struct Samples
    {
        std::map<int64_t, Native> native;
        std::map<int64_t, typename Ros::ConstSharedPtr> ros;
        std::array<int64_t, 2> paired{};
        size_t count = 0;
        int64_t lastNative = 0, lastRos = 0;
    };

    static void require(bool condition, const char *message)
    {
        if (!condition)
            throw std::runtime_error(message);
    }

    static int64_t timestamp(int64_t sec, int64_t nsec)
    {
        require(sec == 0 && nsec > 0 && nsec < 1000000000,
                "ROS/native stamp outside the fixed 1-second run");
        return nsec;
    }

    static int64_t timestamp(const gz::msgs::Header &header)
    {
        require(header.has_stamp(), "native message has no stamp");
        return timestamp(header.stamp().sec(), header.stamp().nsec());
    }

    static void admit(const gz::msgs::Image &msg)
    {
        require(msg.width() == 64 && msg.height() == 48 && msg.step() == 192 &&
                msg.pixel_format_type() == gz::msgs::RGB_INT8 &&
                msg.data().size() == 64 * 48 * 3, "invalid native image for ROS pair");
    }

    static void admit(const Image &msg)
    {
        require(msg.width == 64 && msg.height == 48 && msg.step == 192 &&
                msg.encoding == "rgb8" && msg.is_bigendian == 0 &&
                msg.data.size() == 64 * 48 * 3 &&
                msg.header.frame_id == "fixture_camera", "invalid ROS RGB metadata");
    }

    static void admit(const gz::msgs::LaserScan &msg)
    {
        require(msg.count() == 65 && msg.vertical_count() == 1 &&
                msg.ranges_size() == 65 && msg.intensities_size() == 65,
                "invalid native scan for ROS pair");
    }

    static void admit(const Scan &msg)
    {
        require(msg.ranges.size() == 65 && msg.intensities.size() == 65 &&
                msg.header.frame_id == "fixture_lidar" &&
                msg.time_increment == 0 && msg.scan_time == 0,
                "invalid ROS scan metadata");
    }

    template<class Native, class Ros>
    void capture_native(Samples<Native, Ros> &samples, const Native &msg) noexcept
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (closed_ || error_ || samples.count == 2)
            return;
        try
        {
            admit(msg);
            const auto time = timestamp(msg.header());
            require(time > samples.lastNative, "duplicate/backward native stamp");
            require(samples.native.size() < 24, "native observation capacity exceeded");
            samples.native.emplace(time, msg);
            samples.lastNative = time;
        }
        catch (...) { error_ = std::current_exception(); }
    }

    template<class Native, class Ros>
    void capture_ros(Samples<Native, Ros> &samples, typename Ros::ConstSharedPtr msg) noexcept
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (closed_ || error_ || samples.count == 2)
            return;
        try
        {
            require(static_cast<bool>(msg), "null ROS observation");
            admit(*msg);
            const auto time = timestamp(msg->header.stamp.sec, msg->header.stamp.nanosec);
            require(time > samples.lastRos, "duplicate/backward ROS stamp");
            require(samples.ros.size() < 24, "ROS observation capacity exceeded");
            samples.ros.emplace(time, std::move(msg));
            samples.lastRos = time;
        }
        catch (...) { error_ = std::current_exception(); }
    }

    static void compare(const gz::msgs::Image &native, const Image &ros)
    {
        require(std::equal(ros.data.begin(), ros.data.end(), native.data().begin(),
                [](uint8_t received, char original) {
                    return received == static_cast<unsigned char>(original);
                }), "ROS RGB bytes differ from the original native sample");
    }

    static void same_float(float received, double original)
    {
        static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559);
        const float expected = static_cast<float>(original);
        require(!std::isnan(original) &&
                std::memcmp(&received, &expected, sizeof(float)) == 0,
                "ROS float32 differs from the original native value");
    }

    static void compare(const gz::msgs::LaserScan &native, const Scan &ros)
    {
        // Fixed ros_gz 5912bfe7 sensor_msgs.cpp: native doubles become float32;
        // the one vertical beam is retained, and unsupported timing fields are 0.
        same_float(ros.angle_min, native.angle_min());
        same_float(ros.angle_max, native.angle_max());
        same_float(ros.angle_increment, native.angle_step());
        same_float(ros.range_min, native.range_min());
        same_float(ros.range_max, native.range_max());
        same_float(ros.time_increment, 0);
        same_float(ros.scan_time, 0);
        for (int i = 0; i < 65; ++i)
        {
            same_float(ros.ranges[i], native.ranges(i));
            same_float(ros.intensities[i], native.intensities(i));
        }
    }

    template<class Native, class Ros>
    static void pair(Samples<Native, Ros> &samples)
    {
        for (const auto &[time, native] : samples.native)
        {
            if (samples.count == 2)
                break;
            if (samples.count != 0 && time <= samples.paired[samples.count - 1])
                continue;
            const auto ros = samples.ros.find(time);
            if (ros == samples.ros.end())
                continue;
            compare(native, *ros->second);
            samples.paired[samples.count++] = time;
        }
    }

    bool complete() const
    {
        return images_.count == 2 && scans_.count == 2;
    }

    void check(Deadline deadline)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        check_locked(deadline);
    }

    void check_locked(Deadline deadline)
    {
        require(!closed_, "ROS observer is closed");
        if (error_)
            std::rethrow_exception(error_);
        require(context_ && context_->is_valid(), "ROS observer context stopped");
        require(std::chrono::steady_clock::now() < deadline,
                "ROS observer deadline expired");
    }

    void spin(Deadline deadline)
    {
        check(deadline);
        const auto remaining = std::chrono::duration_cast<std::chrono::nanoseconds>(
            deadline - std::chrono::steady_clock::now());
        require(remaining.count() > 0, "ROS observer deadline expired");
        executor_->spin_once(std::min(remaining,
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::milliseconds(20))));
    }

    std::mutex mutex_;
    Samples<gz::msgs::Image, Image> images_;
    Samples<gz::msgs::LaserScan, Scan> scans_;
    std::exception_ptr error_;
    bool closed_ = false, added_ = false;
    rclcpp::Context::SharedPtr context_;
    rclcpp::Node::SharedPtr node_;
    std::unique_ptr<rclcpp::executors::SingleThreadedExecutor> executor_;
    rclcpp::Subscription<Image>::SharedPtr imageSubscription_;
    rclcpp::Subscription<Scan>::SharedPtr scanSubscription_;
};
