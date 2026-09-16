"""Minimal external backend used by the backend loading smoke test."""


class FixtureBackend:
    def __init__(self, config):
        self.motor_count = config.motor_count
        self.steps = 0
        self.values = [0.0] * self.motor_count

    def step(self, dt):
        assert dt > 0.0
        self.steps += 1
        return (1.0, 2.0, 3.0), (0.0, 0.0, 1.0)

    def set_motor(self, index, value):
        self.values[index] = value

    def reset_motor(self, index):
        self.values[index] = 0.0

    def set_motor_enabled(self, index, enabled):
        del index, enabled

    def set_motor_dm(self, index, command):
        del index, command

    def motor_feedback(self, index):
        return (0.0, 0.0, self.values[index])


def create_backend(config):
    return FixtureBackend(config)
