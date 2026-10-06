#ifndef DM_GAZEBO_RENDERING_HH
#define DM_GAZEBO_RENDERING_HH

#include <atomic>
#include <cmath>
#include <cstdint>
#include <exception>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include <gz/msgs/image.pb.h>
#include <gz/msgs/laserscan.pb.h>
#include <gz/transport/Node.hh>
#include <gz/transport/NodeOptions.hh>

namespace dm_gazebo {

struct CameraFrame {
    int64_t timestampNs;
    uint32_t width, height, step;
    std::string frameId, data;
};

struct LidarScan {
    int64_t timestampNs;
    std::string frameId;
    double angleMin, angleMax, angleStep, rangeMin, rangeMax;
    double verticalAngleMin, verticalAngleMax, verticalAngleStep;
    std::vector<double> ranges, intensities;
};

struct CameraSpec { std::string topic; uint32_t width, height; };
struct LidarSpec {
    std::string topic;
    uint32_t count;
    double angleMin, angleMax, rangeMin, rangeMax;
};

// NativeWorld admits all specs before construction and stops/destroys its Server
// before Close. Subscriptions supply real connections to the standard Sensors
// plugin. These latest-only slots do not bound Gazebo's transport queue or make
// RunOnce a rendering delivery barrier. A new instance has a fresh generation.
class RenderTransport final {
public:
    RenderTransport(const std::map<std::string, CameraSpec> &cameras,
                    const std::map<std::string, LidarSpec> &lidars,
                    const std::string &expectedPartition)
        : state(std::make_shared<State>())
    {
        for (const auto &[name, spec] : cameras) {
            state->cameras.emplace(name, std::nullopt);
            topics.push_back(spec.topic);
        }
        for (const auto &[name, spec] : lidars) {
            state->lidars.emplace(name, std::nullopt);
            topics.push_back(spec.topic);
        }
        if (rendererBusy.test_and_set())
            throw std::runtime_error("another Gazebo rendering owner is active");
        ownsLease = true;
        try {
            const gz::transport::NodeOptions options;
            if (options.Partition() != expectedPartition)
                throw std::runtime_error("Gazebo rendering partition changed");
            node = std::make_unique<gz::transport::Node>(options);
            for (const auto &[name, spec] : cameras) {
                if (!node->Subscribe<gz::msgs::Image>(spec.topic,
                        [shared = state, name, spec](const gz::msgs::Image &msg,
                            const gz::transport::MessageInfo &info) noexcept {
                            Receive(shared, info, [&](State &mailbox) {
                                auto &slot = mailbox.cameras.at(name);
                                const auto time = Stamp(msg.header());
                                if (slot && time <= slot->timestampNs) return;
                                slot = Image(msg, spec, time);
                            });
                        }))
                    throw std::runtime_error("camera subscription failed: " + name);
                ++subscribed;
            }
            for (const auto &[name, spec] : lidars) {
                if (!node->Subscribe<gz::msgs::LaserScan>(spec.topic,
                        [shared = state, name, spec](const gz::msgs::LaserScan &msg,
                            const gz::transport::MessageInfo &info) noexcept {
                            Receive(shared, info, [&](State &mailbox) {
                                auto &slot = mailbox.lidars.at(name);
                                const auto time = Stamp(msg.header());
                                if (slot && time <= slot->timestampNs) return;
                                slot = Scan(msg, spec, time);
                            });
                        }))
                    throw std::runtime_error("lidar subscription failed: " + name);
                ++subscribed;
            }
        } catch (...) {
            const auto primary = std::current_exception();
            try { Close(); }
            catch (const std::exception &cleanup) {
                try { std::rethrow_exception(primary); }
                catch (const std::exception &failure) {
                    std::throw_with_nested(std::runtime_error(std::string(failure.what()) +
                        "; rendering cleanup: " + cleanup.what()));
                }
                catch (...) { std::throw_with_nested(std::runtime_error(cleanup.what())); }
            }
            std::rethrow_exception(primary);
        }
    }

    ~RenderTransport() { try { Close(); } catch (...) {} }
    RenderTransport(const RenderTransport &) = delete;
    RenderTransport &operator=(const RenderTransport &) = delete;

    std::optional<CameraFrame> Camera(const std::string &name) const {
        std::lock_guard<std::mutex> lock(state->mutex);
        Live();
        const auto entry = state->cameras.find(name);
        if (entry == state->cameras.end())
            throw std::invalid_argument("unbound camera: " + name);
        return entry->second;
    }

    std::optional<LidarScan> Lidar(const std::string &name) const {
        std::lock_guard<std::mutex> lock(state->mutex);
        Live();
        const auto entry = state->lidars.find(name);
        if (entry == state->lidars.end())
            throw std::invalid_argument("unbound lidar: " + name);
        return entry->second;
    }

    void CheckError() const {
        std::lock_guard<std::mutex> lock(state->mutex);
        Live();
    }

    void Close() {
        { std::lock_guard<std::mutex> lock(state->mutex); state->closed = true; }
        std::string errors;
        for (size_t i = 0; i < subscribed; ++i) {
            try {
                if (!node->Unsubscribe(topics[i]))
                    throw std::runtime_error("unsubscribe failed: " + topics[i]);
            } catch (const std::exception &failure) {
                if (!errors.empty()) errors += "; ";
                errors += failure.what();
            } catch (...) {
                if (!errors.empty()) errors += "; ";
                errors += "unknown rendering unsubscribe failure";
            }
        }
        subscribed = 0;
        node.reset(); // Queued callbacks capture only the now-closed shared state.
        if (ownsLease) { rendererBusy.clear(); ownsLease = false; }
        if (!errors.empty()) throw std::runtime_error(errors);
    }

private:
    struct State {
        std::mutex mutex;
        bool closed = false;
        std::exception_ptr error;
        std::map<std::string, std::optional<CameraFrame>> cameras;
        std::map<std::string, std::optional<LidarScan>> lidars;
    };

    // Only in-process messages are accepted; callers own topic exclusivity.
    // Equal/older stamped
    // messages are ignored, including their body, rather than replacing a slot.
    // Once a new message fails admission all future snapshots remain frozen.
    template<class Store>
    static void Receive(const std::shared_ptr<State> &shared,
                        const gz::transport::MessageInfo &info, Store store) noexcept {
        try {
            if (!info.IntraProcess()) return;
            std::lock_guard<std::mutex> lock(shared->mutex);
            if (shared->closed || shared->error) return;
            try { store(*shared); }
            catch (...) { shared->error = std::current_exception(); }
        } catch (...) {} // A transport callback must never unwind its worker.
    }

    static int64_t Stamp(const gz::msgs::Header &header) {
        if (!header.has_stamp()) throw std::runtime_error("missing rendering stamp");
        const auto &stamp = header.stamp();
        if (stamp.sec() < 0 || stamp.nsec() < 0 || stamp.nsec() >= 1000000000 ||
            stamp.sec() > (std::numeric_limits<int64_t>::max() - stamp.nsec()) / 1000000000)
            throw std::runtime_error("invalid rendering stamp");
        return stamp.sec() * 1000000000 + stamp.nsec();
    }

    static std::string FrameId(const gz::msgs::Header &header) {
        for (const auto &data : header.data()) {
            if (data.key() != "frame_id" || !data.value_size()) continue;
            if (data.value(0).size() > 1024)
                throw std::runtime_error("native camera frame id exceeds 1024 bytes");
            return data.value(0);
        }
        return {};
    }

    static CameraFrame Image(const gz::msgs::Image &msg, const CameraSpec &spec, int64_t time) {
        const uint64_t bytes = uint64_t(msg.step()) * msg.height();
        if (!msg.width() || !msg.height() || msg.width() != spec.width ||
            msg.height() != spec.height || uint64_t(msg.width()) * 3 != msg.step() ||
            msg.pixel_format_type() != gz::msgs::RGB_INT8 ||
            bytes > 3 * 1024 * 1024 || msg.data().size() != bytes)
            throw std::runtime_error("invalid native RGB image");
        return {time, msg.width(), msg.height(), msg.step(), FrameId(msg.header()), msg.data()};
    }

    static LidarScan Scan(const gz::msgs::LaserScan &msg, const LidarSpec &spec, int64_t time) {
        if (msg.count() < 2 || msg.count() > 4096 || msg.count() != spec.count ||
            msg.vertical_count() != 1 || msg.ranges_size() != int(msg.count()) ||
            msg.intensities_size() != int(msg.count()) ||
            !std::isfinite(msg.angle_min()) || !std::isfinite(msg.angle_max()) ||
            !std::isfinite(msg.angle_step()) || !std::isfinite(msg.range_min()) ||
            !std::isfinite(msg.range_max()) || msg.angle_min() != spec.angleMin ||
            msg.angle_max() != spec.angleMax ||
            msg.angle_step() != (spec.angleMax - spec.angleMin) / (spec.count - 1) ||
            msg.range_min() != spec.rangeMin || msg.range_max() != spec.rangeMax ||
            !std::isfinite(msg.vertical_angle_min()) ||
            !std::isfinite(msg.vertical_angle_max()) ||
            msg.vertical_angle_min() != 0 || msg.vertical_angle_max() != 0 ||
            std::isinf(msg.vertical_angle_step()) || msg.frame().size() > 1024)
            throw std::runtime_error("invalid native lidar geometry");
        for (const auto range : msg.ranges())
            if (std::isnan(range)) throw std::runtime_error("invalid native lidar range");
        return {time, msg.frame(), msg.angle_min(), msg.angle_max(), msg.angle_step(),
            msg.range_min(), msg.range_max(), msg.vertical_angle_min(),
            msg.vertical_angle_max(), msg.vertical_angle_step(),
            {msg.ranges().begin(), msg.ranges().end()},
            {msg.intensities().begin(), msg.intensities().end()}};
    }

    void Live() const {
        if (state->closed) throw std::runtime_error("Gazebo rendering is closed");
        if (state->error) std::rethrow_exception(state->error);
    }

    inline static std::atomic_flag rendererBusy = ATOMIC_FLAG_INIT;
    std::shared_ptr<State> state;
    std::unique_ptr<gz::transport::Node> node;
    std::vector<std::string> topics;
    size_t subscribed = 0;
    bool ownsLease = false;
};

} // namespace dm_gazebo
#endif
