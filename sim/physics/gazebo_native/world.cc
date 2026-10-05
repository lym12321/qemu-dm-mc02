// Thin public-API adapter for Gazebo Sim 8.15.0 (Apache-2.0 upstream).
// Gazebo owns the solver, ECM, reset and threads. No motor or wire semantics.
#include <atomic>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <gz/sim/Joint.hh>
#include <gz/sim/Link.hh>
#include <gz/sim/Model.hh>
#include <gz/sim/Server.hh>
#include <gz/sim/ServerConfig.hh>
#include <gz/sim/System.hh>
#include <gz/sim/World.hh>
#include <gz/sim/Util.hh>
#include <gz/sim/config.hh>
#include <gz/sim/components/AngularVelocity.hh>
#include <gz/sim/components/Imu.hh>
#include <gz/sim/components/LinearAcceleration.hh>
#include <gz/sim/components/Pose.hh>
#include <gz/sensors/ImuSensor.hh>
#include <gz/sensors/SensorFactory.hh>
#include <sdf/Joint.hh>
#include <sdf/JointAxis.hh>
#include <sdf/Link.hh>
#include <sdf/Model.hh>
#include <sdf/Physics.hh>
#include <sdf/Root.hh>
#include <sdf/Imu.hh>
#include <sdf/Sensor.hh>
#include <sdf/World.hh>
#include <sdf/Visual.hh>

namespace sim = gz::sim;
using Bindings = std::map<std::string, std::pair<std::string, std::string>>;
using Samples = std::map<std::string, std::pair<double, double>>;
using BodySample = std::array<double, 7>;
using BodySamples = std::map<std::string, BodySample>;
using ImuTarget = std::tuple<std::string, std::string, std::string>;
using ImuBindings = std::map<std::string, ImuTarget>;
using ImuSample = std::array<double, 6>;
using ImuSamples = std::map<std::string, ImuSample>;

namespace {
bool Finite(const gz::math::Vector3d &value)
{
    return std::isfinite(value.X()) && std::isfinite(value.Y()) &&
           std::isfinite(value.Z());
}

bool Finite(const gz::math::Pose3d &value)
{
    const auto &q = value.Rot();
    return Finite(value.Pos()) && std::isfinite(q.W()) && std::isfinite(q.X()) &&
           std::isfinite(q.Y()) && std::isfinite(q.Z());
}

struct BoundImu {
    sim::Entity entity;
    std::unique_ptr<gz::sensors::ImuSensor> sensor;
};

struct ImuInput {
    gz::math::Pose3d pose;
    gz::math::Vector3d gyro;
    gz::math::Vector3d accel;
};

class Guard final {
public:
    explicit Guard(std::atomic_flag &busy) : busy(busy)
    {
        if (busy.test_and_set(std::memory_order_acquire)) {
            throw std::runtime_error("Gazebo physics operation already in progress");
        }
    }
    ~Guard() { busy.clear(std::memory_order_release); }
private:
    std::atomic_flag &busy;
};

void Name(const std::string &value)
{
    if (value.empty() || value.find('\0') != std::string::npos ||
        value.find("::") != std::string::npos) {
        throw std::invalid_argument("bindings require nonempty unscoped names without NUL");
    }
}

class Observation final : public sim::System,
                          public sim::ISystemConfigure,
                          public sim::ISystemPreUpdate,
                          public sim::ISystemPostUpdate {
public:
    Observation(const Bindings &bindings, const Bindings &bodyBindings,
                const ImuBindings &imuBindings)
        : bindings(bindings), bodyBindings(bodyBindings), imuBindings(imuBindings)
    {
        for (const auto &[name, unused] : this->bindings) {
            (void)unused;
            efforts[name] = 0;
        }
    }

    void Configure(const sim::Entity &, const std::shared_ptr<const sdf::Element> &,
                   sim::EntityComponentManager &ecm, sim::EventManager &) override
    {
        Bind(ecm);
    }

    void Bind(sim::EntityComponentManager &ecm)
    {
        std::map<std::string, sim::Joint> found;
        std::map<std::string, sim::Link> foundBodies;
        std::map<std::string, sim::Entity> foundImus;
        sim::World world(sim::worldEntity(ecm));
        for (const auto &[name, target] : bindings) {
            sim::Model model(world.ModelByName(ecm, target.first));
            sim::Joint joint(model.JointByName(ecm, target.second));
            if (!joint.Valid(ecm) || joint.Type(ecm) != sdf::JointType::REVOLUTE) {
                error = "native revolute binding unavailable: " + name;
                return;
            }
            found.emplace(name, joint);
        }
        for (const auto &[name, target] : bodyBindings) {
            sim::Model model(world.ModelByName(ecm, target.first));
            sim::Link link(model.LinkByName(ecm, target.second));
            if (!link.Valid(ecm)) {
                error = "native body binding unavailable: " + name;
                return;
            }
            foundBodies.emplace(name, link);
        }
        for (const auto &[name, target] : imuBindings) {
            const auto &[modelName, linkName, sensorName] = target;
            sim::Model model(world.ModelByName(ecm, modelName));
            sim::Link link(model.LinkByName(ecm, linkName));
            const auto entity = link.SensorByName(ecm, sensorName);
            if (!ecm.Component<sim::components::Imu>(entity)) {
                error = "native IMU binding unavailable: " + name;
                return;
            }
            foundImus.emplace(name, entity);
        }
        const auto gravity = world.Gravity(ecm);
        if (!foundImus.empty() && (!gravity || !Finite(*gravity))) {
            error = "native IMU gravity unavailable";
            return;
        }
        // Validate every binding before creating observable ECM components.
        joints = std::move(found);
        bodies = std::move(foundBodies);
        for (auto &[name, joint] : joints) {
            (void)name;
            joint.EnablePositionCheck(ecm);
            joint.EnableVelocityCheck(ecm);
        }
        for (auto &[name, link] : bodies) {
            (void)name;
            // Newly requested observation is unavailable until Physics writes
            // it. A default identity pose must not certify an absent engine.
            gz::math::Pose3d unobserved;
            unobserved.Pos().X(std::numeric_limits<double>::quiet_NaN());
            sim::enableComponent<sim::components::WorldPose>(
                ecm, link.Entity(), true, sim::components::WorldPose(unobserved));
        }
        gz::sensors::SensorFactory factory;
        for (const auto &[name, entity] : foundImus) {
            auto data = ecm.Component<sim::components::Imu>(entity)->Data();
            data.SetName(sim::scopedName(entity, ecm, "::", false));
            if (data.Topic().empty()) {
                data.SetTopic(sim::scopedName(entity, ecm) + "/imu");
            }
            auto sensor = factory.CreateSensor<gz::sensors::ImuSensor>(data);
            if (!sensor) {
                error = "native IMU creation failed: " + name;
                return;
            }
            sensor->SetGravity(*gravity);
            imus.emplace(name, BoundImu{entity, std::move(sensor)});
            // SdfEntityCreator creates default-zero sensor observations. Mark
            // output-only components unavailable until Physics writes them.
            const double nan = std::numeric_limits<double>::quiet_NaN();
            gz::math::Pose3d unobserved;
            unobserved.Pos().X(nan);
            ecm.SetComponentData<sim::components::WorldPose>(entity, unobserved);
            ecm.SetComponentData<sim::components::AngularVelocity>(
                entity, gz::math::Vector3d(nan, nan, nan));
            ecm.SetComponentData<sim::components::LinearAcceleration>(
                entity, gz::math::Vector3d(nan, nan, nan));
        }
    }

    void PreUpdate(const sim::UpdateInfo &, sim::EntityComponentManager &ecm) override
    {
        if (!error.empty()) {
            return;
        }
        for (auto &[name, joint] : joints) {
            // Physics clears JointForceCmd after each update. Write all inputs
            // every tick, including zero; SetForce is the sole native producer.
            joint.SetForce(ecm, {efforts.at(name)});
        }
    }

    void PostUpdate(const sim::UpdateInfo &info,
                    const sim::EntityComponentManager &ecm) override
    {
        // Native sensor generation runs on Gazebo's PostUpdate worker. Report
        // errors to the synchronous owner rather than unwinding that thread.
        try {
            Observe(info, ecm);
        } catch (const std::exception &exc) {
            error = std::string("native observation failed: ") + exc.what();
        } catch (...) {
            error = "native observation failed with unknown exception";
        }
    }

    void Observe(const sim::UpdateInfo &info,
                 const sim::EntityComponentManager &ecm)
    {
        if (!error.empty()) {
            return;
        }
        Samples completed;
        BodySamples completedBodies;
        std::map<std::string, ImuInput> imuInputs;
        ImuSamples completedImus;
        for (const auto &[name, joint] : joints) {
            const auto position = joint.Position(ecm);
            const auto velocity = joint.Velocity(ecm);
            if (!position || !velocity || position->size() != 1 ||
                velocity->size() != 1 || !std::isfinite(position->front()) ||
                !std::isfinite(velocity->front())) {
                // PostUpdate may run on a native worker thread; never throw
                // across that callback. The synchronous caller checks error.
                error = "native completed joint sample unavailable: " + name;
                return;
            }
            completed.emplace(name, std::make_pair(position->front(), velocity->front()));
        }
        for (const auto &[name, link] : bodies) {
            const auto pose = link.WorldPose(ecm);
            if (!pose) {
                error = "native completed body pose unavailable: " + name;
                return;
            }
            const auto &p = pose->Pos();
            const auto &q = pose->Rot();
            const BodySample value{p.X(), p.Y(), p.Z(), q.W(), q.X(), q.Y(), q.Z()};
            for (const auto field : value) {
                if (!std::isfinite(field)) {
                    error = "native completed body pose nonfinite: " + name;
                    return;
                }
            }
            completedBodies.emplace(name, value);
        }
        // Admit all completed native kinematics before generating any sample.
        for (const auto &[name, binding] : imus) {
            const auto pose = ecm.Component<sim::components::WorldPose>(binding.entity);
            const auto gyro = ecm.Component<sim::components::AngularVelocity>(binding.entity);
            const auto accel = ecm.Component<sim::components::LinearAcceleration>(binding.entity);
            if (!pose || !gyro || !accel || !Finite(pose->Data()) ||
                !Finite(gyro->Data()) || !Finite(accel->Data())) {
                error = "native completed IMU kinematics unavailable: " + name;
                return;
            }
            imuInputs.emplace(name, ImuInput{pose->Data(), gyro->Data(), accel->Data()});
        }
        for (auto &[name, binding] : imus) {
            const auto &input = imuInputs.at(name);
            auto &sensor = *binding.sensor;
            // gz-sensors8 9348f9fe ImuSensor.cc:232-339 generates in-place,
            // including native gravity. Reseed all raw inputs for every update.
            sensor.SetWorldPose(input.pose);
            sensor.SetAngularVelocity(input.gyro);
            sensor.SetLinearAcceleration(input.accel);
            if (!sensor.Update(info.simTime, true)) {
                error = "native IMU generation failed: " + name;
                return;
            }
            const auto gyro = sensor.AngularVelocity();
            const auto accel = sensor.LinearAcceleration();
            if (!Finite(gyro) || !Finite(accel)) {
                error = "native generated IMU nonfinite: " + name;
                return;
            }
            completedImus.emplace(name, ImuSample{
                gyro.X(), gyro.Y(), gyro.Z(), accel.X(), accel.Y(), accel.Z()});
        }
        samples = std::move(completed);
        bodySamples = std::move(completedBodies);
        imuSamples = std::move(completedImus);
        timeNs = std::chrono::duration_cast<std::chrono::nanoseconds>(info.simTime).count();
        ++observations;
    }

    const Bindings &bindings;
    const Bindings &bodyBindings;
    const ImuBindings &imuBindings;
    std::map<std::string, sim::Joint> joints;
    std::map<std::string, sim::Link> bodies;
    std::map<std::string, BoundImu> imus;
    std::map<std::string, double> efforts;
    Samples samples;
    BodySamples bodySamples;
    ImuSamples imuSamples;
    std::string error;
    int64_t timeNs = 0;
    uint64_t observations = 0;
};

class NativeWorld final {
public:
    NativeWorld(const std::string &path, const Bindings &bindings,
                const Bindings &bodyBindings, const ImuBindings &imuBindings)
        : jointBindings(bindings), bodyBindings(bodyBindings), imuBindings(imuBindings)
    {
        if (path.empty() || path.find('\0') != std::string::npos ||
            (bindings.empty() && bodyBindings.empty() && imuBindings.empty())) {
            throw std::invalid_argument("SDF path and joint/body/IMU bindings must be nonempty");
        }
        sdf::Root root;
        const auto errors = root.Load(path);
        if (!errors.empty()) {
            std::string message = "SDF admission failed";
            for (const auto &error : errors) {
                message += ": " + error.Message();
            }
            throw std::invalid_argument(message);
        }
        if (root.WorldCount() != 1) {
            throw std::invalid_argument("Gazebo joint profile requires exactly one world");
        }
        const auto *world = root.WorldByIndex(0);
        if (world->PhysicsCount() != 1 || world->ActorCount()) {
            throw std::invalid_argument("requires one physics profile and no actors");
        }
        const auto *physics = world->PhysicsByIndex(0);
        const double seconds = physics->MaxStepSize();
        if (!std::isfinite(seconds) || seconds < 1e-9 || seconds > 1) {
            throw std::invalid_argument("SDF timestep must be finite in 1ns..1s");
        }
        // Mirror SimulationRunner's double-seconds -> native duration cast,
        // including fractional-ns truncation; never infer ticks from decimal
        // equality or rewrite the admitted SDF to change native timing.
        tickNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::duration<double>(seconds))).count();
        // This first profile excludes another command owner and default plugin
        // fallback. Native sensor/ROS/world systems need a separate boundary.
        const auto &plugins = world->Plugins();
        if (plugins.size() != 1 || plugins.front().Filename() != "gz-sim-physics-system" ||
            plugins.front().Name() != "gz::sim::systems::Physics") {
            throw std::invalid_argument("world requires only explicit gz-sim-physics-system");
        }
        if (!imuBindings.empty() && !Finite(world->Gravity())) {
            throw std::invalid_argument("IMU world gravity must be finite");
        }
        std::set<ImuTarget> availableImus;
        for (uint64_t i = 0; i < world->ModelCount(); ++i) {
            const auto *model = world->ModelByIndex(i);
            if (!model->Plugins().empty() || model->ModelCount()) {
                throw std::invalid_argument("nested models and model plugins are unsupported");
            }
            for (uint64_t j = 0; j < model->JointCount(); ++j) {
                if (model->JointByIndex(j)->SensorCount()) {
                    throw std::invalid_argument("joint sensors are unsupported");
                }
            }
            for (uint64_t j = 0; j < model->LinkCount(); ++j) {
                const auto *link = model->LinkByIndex(j);
                for (uint64_t k = 0; k < link->SensorCount(); ++k) {
                    const auto *sensor = link->SensorByIndex(k);
                    const auto *imu = sensor->ImuSensor();
                    if (model->Static() || sensor->Type() != sdf::SensorType::IMU ||
                        !imu || !sensor->Plugins().empty() || sensor->EnableMetrics() ||
                        !std::isfinite(sensor->UpdateRate()) || sensor->UpdateRate() != 0) {
                        throw std::invalid_argument("link sensors require bound dynamic IMU, rate zero, no plugins/metrics");
                    }
                    for (const auto *noise : {&imu->LinearAccelerationXNoise(),
                            &imu->LinearAccelerationYNoise(), &imu->LinearAccelerationZNoise(),
                            &imu->AngularVelocityXNoise(), &imu->AngularVelocityYNoise(),
                            &imu->AngularVelocityZNoise()}) {
                        if (noise->Type() != sdf::NoiseType::NONE) {
                            throw std::invalid_argument("IMU noise is unsupported in this profile");
                        }
                    }
                    availableImus.emplace(model->Name(), link->Name(), sensor->Name());
                }
                for (uint64_t k = 0; k < link->VisualCount(); ++k) {
                    if (!link->VisualByIndex(k)->Plugins().empty()) {
                        throw std::invalid_argument("visual plugins are unsupported");
                    }
                }
            }
        }
        std::set<ImuTarget> uniqueImus;
        for (const auto &[name, target] : imuBindings) {
            Name(name);
            Name(std::get<0>(target));
            Name(std::get<1>(target));
            Name(std::get<2>(target));
            if (!availableImus.count(target)) {
                throw std::invalid_argument("requires a dynamic model link IMU: " + name);
            }
            if (!uniqueImus.insert(target).second) {
                throw std::invalid_argument("duplicate native IMU binding");
            }
        }
        if (uniqueImus != availableImus) {
            throw std::invalid_argument("all link sensors require explicit IMU bindings");
        }
        std::set<std::pair<std::string, std::string>> unique;
        for (const auto &[name, target] : bindings) {
            Name(name);
            Name(target.first);
            Name(target.second);
            const auto *model = world->ModelByName(target.first);
            const auto *joint = model ? model->JointByName(target.second) : nullptr;
            if (!model || model->Static() || !joint ||
                joint->Type() != sdf::JointType::REVOLUTE || !joint->Axis()) {
                throw std::invalid_argument("requires a dynamic model revolute joint: " + name);
            }
            if (!unique.insert(target).second) {
                throw std::invalid_argument("duplicate native joint binding");
            }
            const double limit = joint->Axis()->Effort();
            if (!std::isfinite(limit) || limit == 0) {
                throw std::invalid_argument("joint effort limit must be finite positive or negative/unlimited");
            }
            effortLimits[name] = limit;
        }
        unique.clear();
        for (const auto &[name, target] : bodyBindings) {
            Name(name);
            Name(target.first);
            Name(target.second);
            const auto *model = world->ModelByName(target.first);
            if (!model || model->Static() || !model->LinkByName(target.second)) {
                throw std::invalid_argument("requires a dynamic model link: " + name);
            }
            if (!unique.insert(target).second) {
                throw std::invalid_argument("duplicate native body binding");
            }
        }
        // All SDF/binding admission precedes Server services and callbacks.
        config.SetSdfRoot(root);
        Initialize();
    }

    void Initialize()
    {
        try {
            server = std::make_unique<sim::Server>(config);
            observer = std::make_shared<Observation>(jointBindings, bodyBindings, imuBindings);
            server->SetUpdatePeriod(std::chrono::steady_clock::duration::zero());
            if (server->AddSystem(observer) != true) {
                throw std::runtime_error("Gazebo observer admission failed");
            }
            CheckError();
            RunOnce(true);
            if (observer->timeNs != 0) {
                throw std::runtime_error("Gazebo initialization advanced native time");
            }
            failed = false;
        } catch (...) {
            Cleanup();
            throw;
        }
    }

    ~NativeWorld() { Cleanup(); }

    void Close()
    {
        Guard guard(busy);
        closed = true;
        Cleanup();
    }

    void Cleanup()
    {
        failed = true;
        if (server) {
            server->Stop();
            server.reset(); // native destructor joins all owned threads
        }
        observer.reset();
    }

    void Live() const
    {
        if (closed) {
            throw std::runtime_error("Gazebo physics is closed");
        }
        if (failed || !server) {
            throw std::runtime_error("Gazebo physics failed; reset required");
        }
    }

    void CheckError() const
    {
        if (!observer->error.empty()) {
            throw std::runtime_error(observer->error);
        }
    }

    void RunOnce(bool paused)
    {
        const auto before = observer->observations;
        if (!server->RunOnce(paused)) {
            throw std::runtime_error("Gazebo RunOnce failed");
        }
        CheckError();
        if (observer->observations != before + 1) {
            throw std::runtime_error("Gazebo completed observation missing");
        }
    }

    void CheckStep(int64_t dtNs, int64_t timeNs) const
    {
        if (dtNs <= 0 || dtNs % tickNs || dtNs / tickNs > 10000 ||
            timeNs > std::numeric_limits<int64_t>::max() - dtNs) {
            throw std::invalid_argument("step requires 1..10000 whole SDF ticks without time overflow");
        }
    }

    void ValidateStep(int64_t dtNs) const
    {
        Guard guard(busy);
        Live();
        CheckStep(dtNs, observer->timeNs);
    }

    void ValidateSteps(const std::vector<int64_t> &intervals) const
    {
        Guard guard(busy);
        Live();
        if (intervals.empty()) {
            throw std::invalid_argument("step intervals must be nonempty");
        }
        auto target = observer->timeNs;
        for (const auto dtNs : intervals) {
            CheckStep(dtNs, target);
            target += dtNs;
        }
    }

    void Step(int64_t dtNs)
    {
        Guard guard(busy);
        Live();
        CheckStep(dtNs, observer->timeNs);
        const auto target = observer->timeNs + dtNs;
        failed = true;
        for (int64_t i = 0; i < dtNs / tickNs; ++i) {
            RunOnce(false);
        }
        if (observer->timeNs != target) {
            throw std::runtime_error("Gazebo native time did not reach exact target");
        }
        failed = false;
    }

    void CheckEffort(const std::string &name, double value) const
    {
        const auto limit = effortLimits.find(name);
        if (limit == effortLimits.end() || !std::isfinite(value) ||
            (limit->second > 0 && std::abs(value) > limit->second)) {
            throw std::invalid_argument("unbound joint, nonfinite or out-of-limit effort");
        }
    }

    void ValidateEffort(const std::string &name, double value) const
    {
        Guard guard(busy);
        Live();
        CheckEffort(name, value);
    }

    void SetEffort(const std::string &name, double value)
    {
        Guard guard(busy);
        Live();
        CheckEffort(name, value);
        observer->efforts.at(name) = value;
    }

    std::pair<double, double> JointState(const std::string &name) const
    {
        Guard guard(busy);
        Live();
        const auto sample = observer->samples.find(name);
        if (sample == observer->samples.end()) {
            throw std::invalid_argument("unbound joint: " + name);
        }
        return sample->second;
    }

    BodySample BodyPose(const std::string &name) const
    {
        Guard guard(busy);
        Live();
        const auto sample = observer->bodySamples.find(name);
        if (sample == observer->bodySamples.end()) {
            throw std::invalid_argument("unbound body: " + name);
        }
        return sample->second;
    }

    ImuSample Imu(const std::string &name) const
    {
        Guard guard(busy);
        Live();
        const auto sample = observer->imuSamples.find(name);
        if (sample == observer->imuSamples.end()) {
            throw std::invalid_argument("unbound IMU: " + name);
        }
        return sample->second;
    }

    int64_t TimeNs() const { Guard guard(busy); Live(); return observer->timeNs; }
    int64_t TickNs() const { Guard guard(busy); Live(); return tickNs; }

    void Reset()
    {
        Guard guard(busy);
        if (closed) {
            throw std::runtime_error("Gazebo physics is closed");
        }
        // gz-sim8 Physics::ResetPhysics resets q/qd, but not acceleration or
        // engine caches. Recreate the complete native producer from the same
        // admitted SDF snapshot; never zero sensor outputs or integrate warmup.
        Cleanup();
        Initialize();
    }

private:
    sim::ServerConfig config;
    // Sole immutable input owner; observers borrow it across native lifetime.
    Bindings jointBindings;
    Bindings bodyBindings;
    ImuBindings imuBindings;
    std::unique_ptr<sim::Server> server;
    std::shared_ptr<Observation> observer;
    std::map<std::string, double> effortLimits;
    int64_t tickNs = 0;
    bool failed = true;
    bool closed = false;
    // Gazebo releases the Python GIL around its parallel PostUpdate barrier.
    // Reject concurrent/reentrant calls before accessing any owned state.
    mutable std::atomic_flag busy = ATOMIC_FLAG_INIT;
};
} // namespace

PYBIND11_MODULE(_dm_gazebo_native, module)
{
    module.attr("gazebo_version") = GZ_SIM_VERSION_FULL;
    pybind11::class_<NativeWorld>(module, "World")
        .def(pybind11::init<const std::string &, const Bindings &, const Bindings &,
                           const ImuBindings &>(),
             pybind11::arg("path"), pybind11::arg("joints"),
             pybind11::arg("bodies") = Bindings{},
             pybind11::arg("imus") = ImuBindings{})
        .def("step", &NativeWorld::Step)
        .def("validate_step", &NativeWorld::ValidateStep)
        .def("validate_steps", &NativeWorld::ValidateSteps)
        .def("reset", &NativeWorld::Reset)
        .def("close", &NativeWorld::Close)
        .def("set_joint_effort", &NativeWorld::SetEffort)
        .def("validate_joint_effort", &NativeWorld::ValidateEffort)
        .def("get_joint_state", &NativeWorld::JointState)
        .def("get_body_pose", &NativeWorld::BodyPose)
        .def("get_imu", &NativeWorld::Imu)
        .def_property_readonly("time_ns", &NativeWorld::TimeNs)
        .def_property_readonly("timestep_ns", &NativeWorld::TickNs);
}
