// Local SDFormat admission using public SDFormat 14 / Gazebo Sim 8 APIs.
// Native parsing owns includes and geometry; no custom URI resolver or cache.
#pragma once

#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

#include <gz/sim/Util.hh>
#include <sdf/Link.hh>
#include <sdf/Material.hh>
#include <sdf/Model.hh>
#include <sdf/ParserConfig.hh>
#include <sdf/Pbr.hh>
#include <sdf/Root.hh>
#include <sdf/SDFImpl.hh>
#include <sdf/Visual.hh>
#include <sdf/World.hh>

namespace dm_gazebo {
inline sdf::Root LoadSdf(const std::string &path,
                         const std::vector<std::string> &resourcePaths)
{
    // Per-load config: never mutate GlobalConfig or resource-path environment.
    sdf::ParserConfig parser;
    for (const auto &directory : resourcePaths) {
        if (directory.empty() || directory.find('\0') != std::string::npos ||
            directory.find(':') != std::string::npos ||
            !std::filesystem::path(directory).is_absolute() ||
            !std::filesystem::is_directory(directory)) {
            throw std::invalid_argument("resource paths require existing absolute directories without NUL/colon");
        }
        parser.AddURIPath("model://", directory);
    }
    parser.SetStoreResolvedURIs(true);
    sdf::Root root;
    const auto errors = root.Load(path, parser);
    if (!errors.empty()) {
        std::string message = "SDF admission failed";
        for (const auto &error : errors) {
            message += ": " + error.Message();
        }
        throw std::invalid_argument(message);
    }
    // SDFormat 14.9 resolves mesh/script URIs but leaves shader/PBR maps as
    // strings (Material.cc / Pbr.cc). Use the same public native lookup, then
    // store resolved paths in the retained DOM before SetSdfRoot bypasses the
    // normal Gazebo file-loader. Existence is not image decoding/rendering.
    for (uint64_t w = 0; w < root.WorldCount(); ++w) {
        auto *world = root.WorldByIndex(w);
        for (uint64_t m = 0; m < world->ModelCount(); ++m) {
            auto *model = world->ModelByIndex(m);
            for (uint64_t l = 0; l < model->LinkCount(); ++l) {
                auto *link = model->LinkByIndex(l);
                for (uint64_t v = 0; v < link->VisualCount(); ++v) {
                    auto *visual = link->VisualByIndex(v);
                    if (!visual->Material()) {
                        continue;
                    }
                    auto material = *visual->Material();
                    const auto resolve = [&](const std::string &uri) {
                        if (uri.empty()) {
                            return uri;
                        }
                        const auto full = sdf::findFile(gz::sim::asFullPath(
                            uri, material.FilePath()), false, false, parser);
                        if (full.empty() || !std::filesystem::is_regular_file(full)) {
                            throw std::invalid_argument("SDF material resource unavailable: " + uri);
                        }
                        return std::filesystem::absolute(full).lexically_normal().string();
                    };
                    material.SetNormalMap(resolve(material.NormalMap()));
                    if (material.PbrMaterial()) {
                        auto pbr = *material.PbrMaterial();
                        for (const auto type : {sdf::PbrWorkflowType::METAL,
                                                sdf::PbrWorkflowType::SPECULAR}) {
                            if (!pbr.Workflow(type)) {
                                continue;
                            }
                            auto workflow = *pbr.Workflow(type);
                            workflow.SetAlbedoMap(resolve(workflow.AlbedoMap()));
                            workflow.SetNormalMap(resolve(workflow.NormalMap()), workflow.NormalMapType());
                            workflow.SetEnvironmentMap(resolve(workflow.EnvironmentMap()));
                            workflow.SetAmbientOcclusionMap(resolve(workflow.AmbientOcclusionMap()));
                            workflow.SetRoughnessMap(resolve(workflow.RoughnessMap()));
                            workflow.SetMetalnessMap(resolve(workflow.MetalnessMap()));
                            workflow.SetEmissiveMap(resolve(workflow.EmissiveMap()));
                            workflow.SetLightMap(resolve(workflow.LightMap()), workflow.LightMapTexCoordSet());
                            workflow.SetGlossinessMap(resolve(workflow.GlossinessMap()));
                            workflow.SetSpecularMap(resolve(workflow.SpecularMap()));
                            pbr.SetWorkflow(type, workflow);
                        }
                        material.SetPbrMaterial(pbr);
                    }
                    visual->SetMaterial(material);
                }
            }
        }
    }
    return root;
}
} // namespace dm_gazebo
