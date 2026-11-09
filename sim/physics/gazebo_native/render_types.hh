#pragma once

#include <cstdint>
#include <string>
#include <vector>

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
} // namespace dm_gazebo
