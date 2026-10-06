// Resource admission isolation: no Server, Physics, renderer or Python owner.
#include <filesystem>
#include <iostream>
#include <stdexcept>

#include <sdf/Box.hh>
#include <sdf/Collision.hh>
#include <sdf/Geometry.hh>

#include "../resources.hh"

int main(int argc, char **argv)
{
    try {
        if (argc != 3) {
            throw std::runtime_error("requires world and model resource directory");
        }
        const auto globalPaths = sdf::ParserConfig::GlobalConfig().URIPathMap();
        const auto admitted = dm_gazebo::LoadSdf(argv[1], {argv[2]});
        // ServerPrivate's public SetSdfRoot path clones the admitted DOM.
        // Verify the adapted typed material survives that native boundary.
        auto root = admitted.Clone();
        const auto *world = root.WorldByIndex(0);
        const auto *arena = world ? world->ModelByName("rmua19_battlefield") : nullptr;
        if (root.WorldCount() != 1 || world->ModelCount() != 3 ||
            !arena || !arena->Static() || arena->LinkCount() != 12) {
            throw std::runtime_error("native include did not expand the fixed community field");
        }
        const auto *obstacle = arena->LinkByName("obstacle_1");
        const auto *box = obstacle->CollisionByIndex(0)->Geom()->BoxShape();
        // Independent pinned upstream geometry: box 1 x .25 x .4 at z=.2.
        if (!box || box->Size() != gz::math::Vector3d(1, .25, .4) ||
            obstacle->RawPose().Pos().Z() != .2) {
            throw std::runtime_error("native collision geometry differs from source asset");
        }
        const auto texture = std::filesystem::path(argv[2]) /
            "rmua19_battlefield/materials/textures/wood.jpg";
        const auto *pbr = obstacle->VisualByIndex(0)->Material()->PbrMaterial();
        if (!pbr || pbr->Workflow(sdf::PbrWorkflowType::METAL)->AlbedoMap() !=
            std::filesystem::absolute(texture).lexically_normal().string()) {
            throw std::runtime_error("PBR albedo URI not retained as the admitted local file");
        }
        if (sdf::ParserConfig::GlobalConfig().URIPathMap() != globalPaths) {
            throw std::runtime_error("per-load resource admission changed global URI paths");
        }
        std::cout << "PASS native include: static links=12, obstacle top=.4m, resolved wood.jpg\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL native resources: " << error.what() << '\n';
        return 1;
    }
}
