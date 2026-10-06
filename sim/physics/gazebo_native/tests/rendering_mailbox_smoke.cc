// Typed native transport -> production latest-snapshot boundary; no renderer.
#include "../rendering.hh"

#include <gz/msgs/image.pb.h>
#include <gz/msgs/laserscan.pb.h>
#include <gz/transport/Node.hh>

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

namespace
{
void Require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

template<typename Operation>
void RequireThrows(Operation operation, const char *message)
{
    try { operation(); }
    catch (const std::exception &) { return; }
    throw std::runtime_error(message);
}

template<typename Predicate>
void WaitFor(Predicate predicate, const char *message)
{
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
    while (std::chrono::steady_clock::now() < deadline)
    {
        if (predicate())
            return;
        // Polling observes asynchronous native callbacks, not virtual timing.
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    throw std::runtime_error(message);
}

const std::string rgb("\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xff", 12);
const std::map<std::string, dm_gazebo::CameraSpec> cameras{
    {"front", {"/dm_mailbox/image", 2, 2}}};
const std::map<std::string, dm_gazebo::LidarSpec> lidars{
    {"scan", {"/dm_mailbox/scan", 3, -.5, .5, .1, 20}}};

gz::msgs::Image Image(int64_t seconds, int32_t nanoseconds, const std::string &data)
{
    gz::msgs::Image msg;
    auto *header = msg.mutable_header();
    header->mutable_stamp()->set_sec(seconds);
    header->mutable_stamp()->set_nsec(nanoseconds);
    auto *frame = header->add_data();
    frame->set_key("frame_id");
    frame->add_value("robot::camera");
    msg.set_width(2);
    msg.set_height(2);
    msg.set_step(6);
    msg.set_pixel_format_type(gz::msgs::RGB_INT8);
    msg.set_data(data);
    return msg;
}

gz::msgs::LaserScan Scan(int64_t seconds, int32_t nanoseconds)
{
    gz::msgs::LaserScan msg;
    auto *header = msg.mutable_header();
    header->mutable_stamp()->set_sec(seconds);
    header->mutable_stamp()->set_nsec(nanoseconds);
    auto *frame = header->add_data();
    frame->set_key("frame_id");
    frame->add_value("robot::lidar");
    msg.set_frame("robot::lidar");
    msg.set_count(3);
    msg.set_angle_min(-.5);
    msg.set_angle_max(.5);
    msg.set_angle_step(.5);
    msg.set_range_min(.1);
    msg.set_range_max(20);
    msg.set_vertical_count(1);
    msg.set_vertical_angle_min(0);
    msg.set_vertical_angle_max(0);
    // gz-sensors8 Lidar.cc divides by (vertical count-1) for a single row.
    msg.set_vertical_angle_step(std::numeric_limits<double>::quiet_NaN());
    msg.add_ranges(-std::numeric_limits<double>::infinity());
    msg.add_ranges(2.5);
    msg.add_ranges(std::numeric_limits<double>::infinity());
    msg.add_intensities(0);
    msg.add_intensities(7);
    msg.add_intensities(11);
    return msg;
}

void CheckImage(const dm_gazebo::CameraFrame &frame, int64_t timestamp,
                const std::string &data)
{
    Require(frame.timestampNs == timestamp && frame.width == 2 &&
            frame.height == 2 && frame.step == 6 &&
            frame.frameId == "robot::camera" && frame.data == data,
            "camera snapshot lost native stamp, metadata or complete binary bytes");
}

void CheckScan(const dm_gazebo::LidarScan &scan, int64_t timestamp)
{
    Require(scan.timestampNs == timestamp && scan.frameId == "robot::lidar" &&
            scan.angleMin == -.5 && scan.angleMax == .5 && scan.angleStep == .5 &&
            scan.rangeMin == .1 && scan.rangeMax == 20 &&
            scan.verticalAngleMin == 0 && scan.verticalAngleMax == 0 &&
            std::isnan(scan.verticalAngleStep) &&
            scan.ranges.size() == 3 && scan.intensities.size() == 3 &&
            std::isinf(scan.ranges[0]) && scan.ranges[0] < 0 &&
            scan.ranges[1] == 2.5 && std::isinf(scan.ranges[2]) && scan.ranges[2] > 0 &&
            scan.intensities[0] == 0 && scan.intensities[1] == 7 &&
            scan.intensities[2] == 11,
            "lidar snapshot lost native stamp, geometry, infinities or complete vectors");
}

void WaitImage(dm_gazebo::RenderTransport &transport, int64_t timestamp)
{
    WaitFor([&] {
        transport.CheckError();
        const auto frame = transport.Camera("front");
        return frame && frame->timestampNs == timestamp;
    }, "timed out observing the published camera stamp");
}

void WaitScan(dm_gazebo::RenderTransport &transport, int64_t timestamp)
{
    WaitFor([&] {
        transport.CheckError();
        const auto scan = transport.Lidar("scan");
        return scan && scan->timestampNs == timestamp;
    }, "timed out observing the published lidar stamp");
}

void WaitError(dm_gazebo::RenderTransport &transport)
{
    WaitFor([&] {
        try { transport.CheckError(); }
        catch (const std::exception &) { return true; }
        return false;
    }, "malformed new native message did not latch an error");
}
}

int main()
{
    try
    {
        const auto partition = "dm-mailbox-gate-" + std::to_string(getpid());
        Require(setenv("GZ_PARTITION", partition.c_str(), 1) == 0,
                "cannot configure isolated transport partition");
        {
            // Publishers outlive each subscription generation, including Close.
            gz::transport::Node node;
            auto image = node.Advertise<gz::msgs::Image>("/dm_mailbox/image");
            auto scan = node.Advertise<gz::msgs::LaserScan>("/dm_mailbox/scan");
            Require(image && scan, "native typed publishers could not advertise");
            {
                RequireThrows([&] {
                    dm_gazebo::RenderTransport rejected(cameras, lidars, partition + "-wrong");
                }, "mismatched expected transport partition accepted");
                // Successful construction proves rejected admission released its lease.
                dm_gazebo::RenderTransport transport(cameras, lidars, partition);
                Require(!transport.Camera("front") && !transport.Lidar("scan"),
                        "new mailbox fabricated an initial observation");
                RequireThrows([&] { transport.Camera("unknown"); }, "unknown camera accepted");
                RequireThrows([&] { transport.Lidar("unknown"); }, "unknown lidar accepted");
                RequireThrows([&] {
                    dm_gazebo::RenderTransport second(cameras, lidars, partition);
                }, "second simultaneous rendering lease accepted");
                WaitFor([&] { return image.HasConnections() && scan.HasConnections(); },
                        "native publishers have no actual subscriptions");

                Require(image.Publish(Image(0, 0, rgb)) && scan.Publish(Scan(0, 0)),
                        "native zero-stamp publication failed");
                WaitImage(transport, 0);
                WaitScan(transport, 0);
                const auto firstImage = *transport.Camera("front");
                const auto firstScan = *transport.Lidar("scan");
                CheckImage(firstImage, 0, rgb);
                CheckScan(firstScan, 0);

                const std::string newer(12, '\x7f');
                Require(image.Publish(Image(2, 3, newer)) && scan.Publish(Scan(2, 4)),
                        "native positive-stamp publication failed");
                WaitImage(transport, 2000000003);
                WaitScan(transport, 2000000004);
                CheckImage(*transport.Camera("front"), 2000000003, newer);
                CheckScan(*transport.Lidar("scan"), 2000000004);
                CheckImage(firstImage, 0, rgb);
                CheckScan(firstScan, 0);
                auto callerImage = *transport.Camera("front");
                auto callerScan = *transport.Lidar("scan");
                callerImage.data[0] = '\0';
                callerScan.ranges[1] = 99;
                CheckImage(*transport.Camera("front"), 2000000003, newer);
                CheckScan(*transport.Lidar("scan"), 2000000004);

                // Native local publication queues messages in order. The subsequent
                // accepted stamp observes callbacks after these malformed stale ones.
                auto staleScan = Scan(2, 4);
                staleScan.set_ranges(1, std::numeric_limits<double>::quiet_NaN());
                Require(image.Publish(Image(2, 3, "")) && image.Publish(Image(0, 0, "")) &&
                        image.Publish(Image(3, 1, rgb)) && scan.Publish(staleScan),
                        "native stale-message publication failed");
                staleScan.mutable_header()->mutable_stamp()->set_sec(0);
                staleScan.mutable_header()->mutable_stamp()->set_nsec(0);
                Require(scan.Publish(staleScan) && scan.Publish(Scan(3, 2)),
                        "native lidar replacement publication failed");
                WaitImage(transport, 3000000001);
                WaitScan(transport, 3000000002);
                CheckImage(*transport.Camera("front"), 3000000001, rgb);
                CheckScan(*transport.Lidar("scan"), 3000000002);

                Require(image.Publish(Image(4, 0, "short")),
                        "native malformed camera publication failed");
                WaitError(transport);
                RequireThrows([&] { transport.Camera("front"); },
                              "camera getter ignored the retained callback error");
                RequireThrows([&] { transport.Lidar("scan"); },
                              "lidar getter ignored the global camera callback error");
                // This may still be queued at Close; it must not be inherited
                // by the new subscription generation below.
                Require(image.Publish(Image(5, 0, rgb)), "native post-error publication failed");
                RequireThrows([&] { transport.CheckError(); },
                              "retained callback error disappeared");
                transport.Close();
                transport.Close();
                RequireThrows([&] { transport.Camera("front"); }, "closed camera getter accepted");
                RequireThrows([&] { transport.Lidar("scan"); }, "closed lidar getter accepted");
                RequireThrows([&] { transport.CheckError(); }, "closed error check accepted");
            }
            {
                dm_gazebo::RenderTransport transport(cameras, lidars, partition);
                Require(!transport.Camera("front") && !transport.Lidar("scan"),
                        "new subscription generation inherited an old snapshot");
                WaitFor([&] { return image.HasConnections() && scan.HasConnections(); },
                        "new rendering lease failed to subscribe");
                Require(image.Publish(Image(0, 0, rgb)), "new-generation zero stamp failed");
                WaitImage(transport, 0);
                CheckImage(*transport.Camera("front"), 0, rgb);
                Require(image.Publish(Image(1, 1000000000, rgb)),
                        "native noncanonical timestamp publication failed");
                WaitError(transport);
                RequireThrows([&] { transport.CheckError(); }, "noncanonical stamp error lost");
                transport.Close();
            }
            {
                dm_gazebo::RenderTransport transport(cameras, lidars, partition);
                WaitFor([&] { return scan.HasConnections(); }, "lidar did not reconnect");
                Require(scan.Publish(Scan(0, 0)), "new-generation lidar zero stamp failed");
                WaitScan(transport, 0);
                auto invalid = Scan(1, 0);
                invalid.set_ranges(1, std::numeric_limits<double>::quiet_NaN());
                Require(scan.Publish(invalid), "native NaN range publication failed");
                WaitError(transport);
                RequireThrows([&] { transport.Lidar("scan"); }, "NaN range was accepted");
                transport.Close();
            }
        } // Publisher Node and all subscription generations are destroyed.
        std::cout << "PASS native rendering mailbox: complete typed snapshots, freshness, "
                     "errors, copies and lease recreation; subscriptions cleaned\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "FAIL native rendering mailbox: " << error.what() << '\n';
        return 1;
    }
}
