// Included once by world.cc after its canonical native implementation.
#include "sim-host/src/physics/gazebo/ffi.rs.h"

namespace dm_gazebo::rust_bridge {
namespace {
Bindings Import(rust::Slice<const Binding> requested)
{
    Bindings result;
    for (const auto &binding : requested) {
        if (!result.emplace(std::string(binding.alias),
                std::make_pair(std::string(binding.model),
                               std::string(binding.element))).second) {
            throw std::invalid_argument("duplicate binding alias");
        }
    }
    return result;
}
ImuBindings Import(rust::Slice<const SensorBinding> requested)
{
    ImuBindings result;
    for (const auto &binding : requested) {
        if (!result.emplace(std::string(binding.alias),
                std::make_tuple(std::string(binding.model),
                    std::string(binding.link), std::string(binding.sensor))).second) {
            throw std::invalid_argument("duplicate IMU binding alias");
        }
    }
    return result;
}
std::vector<std::string> Import(rust::Slice<const rust::String> requested)
{
    std::vector<std::string> result;
    for (const auto &path : requested) result.emplace_back(path);
    return result;
}
} // namespace

class World::Impl {
public:
    Impl(rust::Str path, rust::Slice<const Binding> joints,
         rust::Slice<const Binding> bodies, rust::Slice<const SensorBinding> imus,
         rust::Slice<const rust::String> resourcePaths,
         rust::Slice<const SensorBinding> cameras, rust::Slice<const SensorBinding> lidars)
        : world(std::string(path), Import(joints), Import(bodies), Import(imus),
                Import(resourcePaths), Import(cameras), Import(lidars)) {}
    NativeWorld world;
};

World::World(rust::Str path, rust::Slice<const Binding> joints,
             rust::Slice<const Binding> bodies, rust::Slice<const SensorBinding> imus,
             rust::Slice<const rust::String> resourcePaths,
             rust::Slice<const SensorBinding> cameras, rust::Slice<const SensorBinding> lidars)
    : impl(std::make_unique<Impl>(path, joints, bodies, imus, resourcePaths, cameras, lidars)) {}
World::~World() = default;
std::unique_ptr<World> new_world(rust::Str path, rust::Slice<const Binding> joints,
    rust::Slice<const Binding> bodies, rust::Slice<const SensorBinding> imus,
    rust::Slice<const rust::String> resourcePaths,
    rust::Slice<const SensorBinding> cameras, rust::Slice<const SensorBinding> lidars)
{
    return std::make_unique<World>(path, joints, bodies, imus, resourcePaths, cameras, lidars);
}
rust::String version() { return GZ_SIM_VERSION_FULL; }
int64_t World::time_ns() const { return impl->world.TimeNs(); }
int64_t World::timestep_ns() const { return impl->world.TickNs(); }
void World::validate_step(int64_t dtNs) const { impl->world.ValidateStep(dtNs); }
void World::validate_steps(rust::Slice<const int64_t> intervals) const
{
    impl->world.ValidateSteps({intervals.begin(), intervals.end()});
}
void World::validate_joint_effort(rust::Str name, double effort) const
{
    impl->world.ValidateEffort(std::string(name), effort);
}
JointSample World::get_joint_state(rust::Str name) const
{
    const auto sample = impl->world.JointState(std::string(name));
    return {sample.first, sample.second};
}
BodySample World::get_body_pose(rust::Str name) const
{
    const auto sample = impl->world.BodyPose(std::string(name));
    return {{sample[0], sample[1], sample[2]},
            {sample[3], sample[4], sample[5], sample[6]}};
}
ImuSample World::get_imu(rust::Str name) const
{
    const auto sample = impl->world.Imu(std::string(name));
    return {{sample[0], sample[1], sample[2]}, {sample[3], sample[4], sample[5]}};
}
CameraSample World::get_camera(rust::Str name) const
{
    const auto frame = impl->world.Camera(std::string(name));
    if (!frame) return {};
    rust::Vec<uint8_t> data;
    data.reserve(frame->data.size());
    for (const auto byte : frame->data) data.push_back(static_cast<uint8_t>(byte));
    return {true, frame->timestampNs, frame->width, frame->height, frame->step,
            frame->frameId, std::move(data)};
}
LidarSample World::get_lidar(rust::Str name) const
{
    const auto scan = impl->world.Lidar(std::string(name));
    if (!scan) return {};
    rust::Vec<double> ranges, intensities;
    ranges.reserve(scan->ranges.size());
    intensities.reserve(scan->intensities.size());
    for (const auto value : scan->ranges) ranges.push_back(value);
    for (const auto value : scan->intensities) intensities.push_back(value);
    return {true, scan->timestampNs, scan->frameId, scan->angleMin, scan->angleMax,
            scan->angleStep, scan->rangeMin, scan->rangeMax, scan->verticalAngleMin,
            scan->verticalAngleMax, scan->verticalAngleStep,
            std::move(ranges), std::move(intensities)};
}
void World::set_joint_effort(rust::Str name, double effort)
{
    impl->world.SetEffort(std::string(name), effort);
}
void World::step(int64_t dtNs) { impl->world.Step(dtNs); }
void World::reset() { impl->world.Reset(); }
void World::close() { impl->world.Close(); }
} // namespace dm_gazebo::rust_bridge
