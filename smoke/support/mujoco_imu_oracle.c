/* Independent native observation for the compiled worker's firmware consumer. */
#include <mujoco/mujoco.h>
#include <stdio.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    char error[1024] = {0};
    mjModel *model = mj_loadXML(argv[1], NULL, error, sizeof error);
    if (!model) { fprintf(stderr, "%s\n", error); return 1; }
    mjData *data = mj_makeData(model);
    if (!data) { mj_deleteModel(model); return 1; }
    mj_forward(model, data);
    mj_step(model, data);
    mj_forward(model, data);
    const int gyro = mj_name2id(model, mjOBJ_SENSOR, "imu_gyro");
    const int accel = mj_name2id(model, mjOBJ_SENSOR, "imu_accel");
    if (gyro < 0 || accel < 0) {
        mj_deleteData(data);
        mj_deleteModel(model);
        return 1;
    }
    printf("[%.17g", data->time);
    for (int i = 0; i < 3; ++i) printf(",%.17g", data->sensordata[model->sensor_adr[gyro] + i]);
    for (int i = 0; i < 3; ++i) printf(",%.17g", data->sensordata[model->sensor_adr[accel] + i]);
    puts("]");
    mj_deleteData(data);
    mj_deleteModel(model);
    return 0;
}
