// Rust/CXX lifetime and value adapter over the canonical NativeWorld.
#pragma once

#include "rust/cxx.h"
#include <array>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>

// CXX's documented exception hook preserves pure admission vs native failures.
namespace rust::behavior {
template <typename Try, typename Fail>
static void trycatch(Try &&operation, Fail &&fail) noexcept
{
    try { operation(); }
    catch (const std::invalid_argument &error) {
        fail((std::string("admission: ") + error.what()).c_str());
    }
    catch (const std::exception &error) { fail(error.what()); }
    catch (...) { fail("unknown Gazebo native exception"); }
}
} // namespace rust::behavior

namespace dm_gazebo::rust_bridge {
struct Binding;
struct SensorBinding;
struct JointSample;
struct BodySample;
struct ImuSample;
struct CameraSample;
struct LidarSample;

class World final {
public:
    World(rust::Str path, rust::Slice<const Binding> joints,
          rust::Slice<const Binding> bodies, rust::Slice<const SensorBinding> imus,
          rust::Slice<const rust::String> resourcePaths,
          rust::Slice<const SensorBinding> cameras, rust::Slice<const SensorBinding> lidars);
    ~World();
    int64_t time_ns() const;
    int64_t timestep_ns() const;
    void validate_step(int64_t dtNs) const;
    void validate_steps(rust::Slice<const int64_t> intervals) const;
    void validate_joint_effort(rust::Str name, double effort) const;
    JointSample get_joint_state(rust::Str name) const;
    BodySample get_body_pose(rust::Str name) const;
    ImuSample get_imu(rust::Str name) const;
    CameraSample get_camera(rust::Str name) const;
    LidarSample get_lidar(rust::Str name) const;
    void set_joint_effort(rust::Str name, double effort);
    void step(int64_t dtNs);
    void reset();
    void close();
private:
    class Impl;
    std::unique_ptr<Impl> impl;
};

std::unique_ptr<World> new_world(rust::Str path, rust::Slice<const Binding> joints,
    rust::Slice<const Binding> bodies, rust::Slice<const SensorBinding> imus,
    rust::Slice<const rust::String> resourcePaths,
    rust::Slice<const SensorBinding> cameras, rust::Slice<const SensorBinding> lidars);
rust::String version();
} // namespace dm_gazebo::rust_bridge
