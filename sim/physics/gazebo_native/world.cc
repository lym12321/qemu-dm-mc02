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
#include <gz/sim/components/Pose.hh>
#include <sdf/Joint.hh>
#include <sdf/JointAxis.hh>
#include <sdf/Link.hh>
#include <sdf/Model.hh>
#include <sdf/Physics.hh>
#include <sdf/Root.hh>
#include <sdf/World.hh>
#include <sdf/Visual.hh>

namespace sim = gz::sim;
using Bindings = std::map<std::string, std::pair<std::string, std::string>>;
using Samples = std::map<std::string, std::pair<double, double>>;
using BodySample = std::array<double, 7>;
using BodySamples = std::map<std::string, BodySample>;

namespace {
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
                          public sim::ISystemPostUpdate,
                          public sim::ISystemReset {
public:
    Observation(Bindings bindings, Bindings bodyBindings)
        : bindings(std::move(bindings)), bodyBindings(std::move(bodyBindings))
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
        if (!error.empty()) {
            return;
        }
        Samples completed;
        BodySamples completedBodies;
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
        samples = std::move(completed);
        bodySamples = std::move(completedBodies);
        timeNs = std::chrono::duration_cast<std::chrono::nanoseconds>(info.simTime).count();
        ++observations;
    }

    void Reset(const sim::UpdateInfo &, sim::EntityComponentManager &ecm) override
    {
        error.clear();
        samples.clear();
        bodySamples.clear();
        for (auto &[name, value] : efforts) {
            (void)name;
            value = 0;
        }
        // Native initial ECM predates our Configure-created components.
        Bind(ecm);
        ++resets;
    }

    Bindings bindings;
    Bindings bodyBindings;
    std::map<std::string, sim::Joint> joints;
    std::map<std::string, sim::Link> bodies;
    std::map<std::string, double> efforts;
    Samples samples;
    BodySamples bodySamples;
    std::string error;
    int64_t timeNs = 0;
    uint64_t observations = 0;
    uint64_t resets = 0;
};

class NativeWorld final {
public:
    NativeWorld(const std::string &path, const Bindings &bindings,
                const Bindings &bodyBindings)
    {
        if (path.empty() || path.find('\0') != std::string::npos ||
            (bindings.empty() && bodyBindings.empty())) {
            throw std::invalid_argument("SDF path and joint/body bindings must be nonempty");
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
                if (link->SensorCount()) {
                    throw std::invalid_argument("link sensors are unsupported");
                }
                for (uint64_t k = 0; k < link->VisualCount(); ++k) {
                    if (!link->VisualByIndex(k)->Plugins().empty()) {
                        throw std::invalid_argument("visual plugins are unsupported");
                    }
                }
            }
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
        sim::ServerConfig config;
        config.SetSdfRoot(root);
        server = std::make_unique<sim::Server>(config);
        observer = std::make_shared<Observation>(bindings, bodyBindings);
        try {
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
        if (!server) {
            throw std::runtime_error("Gazebo physics is closed");
        }
        if (failed) {
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

    int64_t TimeNs() const { Guard guard(busy); Live(); return observer->timeNs; }
    int64_t TickNs() const { Guard guard(busy); Live(); return tickNs; }

    void Reset()
    {
        Guard guard(busy);
        if (!server) {
            throw std::runtime_error("Gazebo physics is closed");
        }
        failed = true;
        const auto resetCount = observer->resets;
        if (server->Reset(0) != true) {
            throw std::runtime_error("Gazebo reset request rejected");
        }
        // Native request processing, ECM/ISystemReset, then completed state.
        // The reset iteration does not execute PostUpdate; don't invent one.
        for (int i = 0; i < 3; ++i) {
            if (!server->RunOnce(true)) {
                throw std::runtime_error("Gazebo paused reset update failed");
            }
        }
        // The first paused update can still observe a failed old state. Only
        // adjudicate after native ISystemReset and fresh PostUpdate projection.
        CheckError();
        if (observer->resets != resetCount + 1 || observer->timeNs != 0 ||
            observer->samples.size() != effortLimits.size() ||
            observer->bodySamples.size() != observer->bodyBindings.size()) {
            throw std::runtime_error("Gazebo reset did not complete at native time zero");
        }
        failed = false;
    }

private:
    std::unique_ptr<sim::Server> server;
    std::shared_ptr<Observation> observer;
    std::map<std::string, double> effortLimits;
    int64_t tickNs = 0;
    bool failed = true;
    // Gazebo releases the Python GIL around its parallel PostUpdate barrier.
    // Reject concurrent/reentrant calls before accessing any owned state.
    mutable std::atomic_flag busy = ATOMIC_FLAG_INIT;
};
} // namespace

PYBIND11_MODULE(_dm_gazebo_native, module)
{
    module.attr("gazebo_version") = GZ_SIM_VERSION_FULL;
    pybind11::class_<NativeWorld>(module, "World")
        .def(pybind11::init<const std::string &, const Bindings &, const Bindings &>(),
             pybind11::arg("path"), pybind11::arg("joints"),
             pybind11::arg("bodies") = Bindings{})
        .def("step", &NativeWorld::Step)
        .def("validate_step", &NativeWorld::ValidateStep)
        .def("validate_steps", &NativeWorld::ValidateSteps)
        .def("reset", &NativeWorld::Reset)
        .def("close", &NativeWorld::Close)
        .def("set_joint_effort", &NativeWorld::SetEffort)
        .def("validate_joint_effort", &NativeWorld::ValidateEffort)
        .def("get_joint_state", &NativeWorld::JointState)
        .def("get_body_pose", &NativeWorld::BodyPose)
        .def_property_readonly("time_ns", &NativeWorld::TimeNs)
        .def_property_readonly("timestep_ns", &NativeWorld::TickNs);
}
