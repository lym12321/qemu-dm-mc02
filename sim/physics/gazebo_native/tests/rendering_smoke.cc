// Test-only native Sensors -> Gazebo transport gate; no adapter-owned renderer.
#include <gz/msgs/image.pb.h>
#include <gz/msgs/laserscan.pb.h>
#include <gz/sim/Server.hh>
#include <gz/sim/ServerConfig.hh>
#include <gz/transport/Node.hh>

#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <unistd.h>

#ifdef DM_GAZEBO_WITH_ROS
#include "ros_observer.hh"
#endif

namespace
{
void require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

int64_t stamp(const gz::msgs::Header &header)
{
    require(header.has_stamp(), "missing native sample stamp");
    const auto &time = header.stamp();
    require(time.sec() == 0 && time.nsec() > 0 && time.nsec() < 1000000000,
            "sample outside the fixed 1-second native run");
    return time.nsec();
}

// Keep only two observations per sensor. Callbacks never unwind transport.
struct Observations
{
#ifdef DM_GAZEBO_WITH_ROS
    RosObserver *ros = nullptr; // Borrowed; outlives native transport callbacks.
#endif
    std::mutex mutex;
    std::condition_variable changed;
    std::array<int64_t, 2> images{}, scans{};
    size_t imageCount = 0, scanCount = 0;
    std::string error;
    int red = 0, green = 0, blue = 0;
    double centreRange = 0;

    void image(const gz::msgs::Image &msg)
    {
#ifdef DM_GAZEBO_WITH_ROS
        if (ros)
            ros->native_image(msg);
#endif
        std::lock_guard<std::mutex> lock(mutex);
        if (imageCount == images.size() || !error.empty())
            return;
        try
        {
            require(msg.width() == 64 && msg.height() == 48 && msg.step() == 192 &&
                    msg.pixel_format_type() == gz::msgs::RGB_INT8 &&
                    msg.data().size() == 64 * 48 * 3, "invalid native RGB image");
            // The +X-facing sensor sees the red box at its centre, black outside.
            const size_t pixel = (24 * 64 + 32) * 3;
            red = static_cast<unsigned char>(msg.data()[pixel]);
            green = static_cast<unsigned char>(msg.data()[pixel + 1]);
            blue = static_cast<unsigned char>(msg.data()[pixel + 2]);
            require(red > 100 && green < 30 && blue < 30,
                    "camera centre does not see the red target");
            require(static_cast<unsigned char>(msg.data()[0]) < 30 &&
                    static_cast<unsigned char>(msg.data()[1]) < 30 &&
                    static_cast<unsigned char>(msg.data()[2]) < 30,
                    "camera corner does not see the black background");
            const auto time = stamp(msg.header());
            require(imageCount == 0 || time > images[imageCount - 1],
                    "camera stamp did not advance");
            images[imageCount++] = time;
        }
        catch (const std::exception &failure) { error = failure.what(); }
        changed.notify_one();
    }

    void scan(const gz::msgs::LaserScan &msg)
    {
#ifdef DM_GAZEBO_WITH_ROS
        if (ros)
            ros->native_scan(msg);
#endif
        std::lock_guard<std::mutex> lock(mutex);
        if (scanCount == scans.size() || !error.empty())
            return;
        try
        {
            require(msg.count() == 65 && msg.vertical_count() == 1 &&
                    msg.ranges_size() == 65, "invalid native lidar dimensions");
            require(std::abs(msg.angle_min() + 0.3) < 1e-6 &&
                    std::abs(msg.angle_max() - 0.3) < 1e-6 &&
                    std::abs(msg.angle_step() - 0.6 / 64) < 1e-6 &&
                    std::abs(msg.range_min() - 0.1) < 1e-6 &&
                    std::abs(msg.range_max() - 10) < 1e-6,
                    "invalid native lidar units/geometry");
            // Independent scene geometry: front face is 3 - 1/2 = 2.5 metres.
            centreRange = msg.ranges(32);
            require(std::isfinite(centreRange) && std::abs(centreRange - 2.5) < 0.02,
                    "lidar centre does not hit the 2.5-metre target");
            require(std::isinf(msg.ranges(0)) && msg.ranges(0) > 0 &&
                    std::isinf(msg.ranges(64)) && msg.ranges(64) > 0,
                    "lidar outer rays should miss the box");
            const auto time = stamp(msg.header());
            require(scanCount == 0 || time > scans[scanCount - 1],
                    "lidar stamp did not advance");
            scans[scanCount++] = time;
        }
        catch (const std::exception &failure) { error = failure.what(); }
        changed.notify_one();
    }
};
}

int main(int argc, char **argv)
{
    bool withRos = false;
#ifdef DM_GAZEBO_WITH_ROS
    withRos = argc == 3 && std::string(argv[2]) == "--ros";
    std::unique_ptr<RosObserver> ros; // Released after Node and Observations.
#endif
    // Per-process discovery domain, also isolated from interactive Gazebo worlds.
    const auto partition = "dm-render-gate-" + std::to_string(getpid());
    if ((!withRos && setenv("GZ_PARTITION", partition.c_str(), 1) != 0) ||
            unsetenv("DISPLAY") != 0)
    {
        std::cerr << "FAIL: cannot configure isolated headless test environment\n";
        return 1;
    }
    Observations observations;
    gz::transport::Node node;
    std::unique_ptr<gz::sim::Server> server;
    int result = 1;
    try
    {
        require(argc == 2 || withRos, "usage: gazebo-rendering-smoke rendering.sdf [--ros]");
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(12);
#ifdef DM_GAZEBO_WITH_ROS
        if (withRos)
        {
            require(getenv("GZ_PARTITION") && *getenv("GZ_PARTITION"),
                    "ROS gate requires its owner's isolated GZ_PARTITION");
            ros = std::make_unique<RosObserver>();
            observations.ros = ros.get();
            ros->wait_ready(deadline);
        }
#endif
        require(node.Subscribe("/dm_render/image", &Observations::image, &observations) &&
                node.Subscribe("/dm_render/scan", &Observations::scan, &observations),
                "native transport subscription failed");
        gz::sim::ServerConfig config;
        config.SetSdfFile(argv[1]);
        config.SetHeadlessRendering(true); // Native Ogre2 EGL; no X server.
        server = std::make_unique<gz::sim::Server>(config);
        server->SetUpdatePeriod(std::chrono::milliseconds(1));
        require(server->Run(false, 1000, false), "native Server failed to start");
#ifdef DM_GAZEBO_WITH_ROS
        if (withRos)
        {
            bool complete = false;
            do
            {
                complete = ros->pump(deadline);
                std::lock_guard<std::mutex> lock(observations.mutex);
                require(observations.error.empty(), observations.error.c_str());
                complete = complete && observations.imageCount == 2 && observations.scanCount == 2;
            } while (!complete);
        }
        else
#endif
        {
            std::unique_lock<std::mutex> lock(observations.mutex);
            const bool ready = observations.changed.wait_until(lock, deadline, [&] {
                return !observations.error.empty() ||
                    (observations.imageCount == 2 && observations.scanCount == 2);
            });
            require(observations.error.empty(), observations.error.c_str());
            require(ready && std::chrono::steady_clock::now() < deadline,
                    "timed out waiting for two real camera/lidar messages");
        }
        result = 0;
    }
    catch (const std::exception &failure)
    {
        std::cerr << "FAIL: " << failure.what() << '\n';
    }
    // Native Stop/destruction joins render/simulation workers before callback
    // state release. Driver calls/join are bounded externally by CTest TIMEOUT.
    if (server)
        server->Stop();
    server.reset();
    const bool imageClosed = node.Unsubscribe("/dm_render/image");
    const bool scanClosed = node.Unsubscribe("/dm_render/scan");
    if (!imageClosed || !scanClosed)
    {
        std::cerr << "FAIL: native transport unsubscribe failed\n";
        result = 1;
    }
#ifdef DM_GAZEBO_WITH_ROS
    if (ros)
    {
        try { ros->close(); }
        catch (const std::exception &failure)
        {
            std::cerr << "FAIL: ROS cleanup: " << failure.what() << '\n';
            result = 1;
        }
        ros->report(std::cout);
        std::cout << '\n';
    }
#endif
    std::lock_guard<std::mutex> lock(observations.mutex);
    if (result != 0)
        std::cerr << "Observed images=" << observations.imageCount
            << ", scans=" << observations.scanCount << '\n';
    if (result == 0)
    {
        std::cout << "PASS: native RGB 64x48 centre=" << observations.red << ','
            << observations.green << ',' << observations.blue << " stamps_ns="
            << observations.images[0] << ',' << observations.images[1]
            << "; lidar 65x1 centre_m=" << observations.centreRange << " stamps_ns="
            << observations.scans[0] << ',' << observations.scans[1]
            << "; Server and subscriptions cleaned\n";
    }
    return result;
}
