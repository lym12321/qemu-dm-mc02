"""Test-only trace around the existing MuJoCo adapter."""

import json
import os
import time

from dm_mc02_sim_worker import MujocoEngine


class BenchEngine(MujocoEngine):
    def __init__(self, config):
        self.max_events = int(os.environ.get("DM_TROBOT_BENCH_MAX_EVENTS", "50000"))
        if self.max_events <= 0:
            raise ValueError("business bench trace budget must be positive")
        super().__init__(config.mujoco_model, config.motor_count,
                         1.0 / config.rate, config.dm_t_max)
        self.trace = open(os.environ["DM_TROBOT_BENCH_TRACE"], "w",
                          encoding="utf-8")
        self.events = 0

    def record(self, event, **fields):
        if self.events >= self.max_events:
            raise BufferError(f"business bench trace exceeded {self.max_events} events")
        self.events += 1
        self.trace.write(json.dumps(dict(
            event=event, plant_time_s=float(self.data.time),
            wall_time_ns=time.monotonic_ns(), **fields)) + "\n")

    def step(self, dt):
        imu = super().step(dt)
        position, velocity, effort = self.motor_feedback(0)
        self.record("step", dt=dt, position=position, velocity=velocity,
                    effort=effort, gyro=imu[0], accel=imu[1])
        return imu

    def set_motor_dm(self, index, command):
        super().set_motor_dm(index, command)
        self.record("control", index=index, **command)

    def set_motor_enabled(self, index, enabled):
        super().set_motor_enabled(index, enabled)
        self.record("enabled", index=index, enabled=enabled)

    def reset_motor(self, index):
        super().reset_motor(index)
        self.record("clear-error", index=index)

    def reset(self):
        super().reset()
        self.record("reset")

    def close(self):
        self.trace.close()


def create_backend(config):
    return BenchEngine(config)
