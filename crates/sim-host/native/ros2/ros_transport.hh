#pragma once
#include "rust/cxx.h"
#include <cstddef>
#include <cstdint>
#include <memory>

namespace dm_ros2 {
struct Snapshot;
class Transport {
public:
    Transport(std::size_t motor_count, rust::Str motor_topic, rust::Str imu_topic,
        rust::Str joint_state_topic, rust::Str node_name, rust::Str reset_service);
    ~Transport() noexcept;
    Transport(const Transport &) = delete;
    Transport &operator=(const Transport &) = delete;
    Snapshot sample() const;
    void publish_efforts(rust::Slice<const double> values);
    void pump(std::uint64_t timeout_ns);
    void reset(std::uint64_t timeout_ns);
    void close();
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
std::unique_ptr<Transport> new_transport(std::size_t motor_count,
    rust::Str motor_topic, rust::Str imu_topic, rust::Str joint_state_topic,
    rust::Str node_name, rust::Str reset_service);
}
