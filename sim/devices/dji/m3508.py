"""Virtual M3508 using the C620 CAN subset and a separate simple rotor.

The default J=.02 kg m², damping=.08 Nm/(rad/s), .02 Nm/A torque coefficient,
25 Celsius and 100 ms command timeout are uncalibrated model assumptions.
Feedback is emitted at most once per explicit step: missed 1 kHz samples are
coalesced into the latest state. Encoder uses floor after rotor wrap; RPM uses
nearest integer (ties to even) and saturates to signed16. Current feedback is
the instantaneous commanded current, not an electrical measurement. There is
no gearbox conversion; the real firmware decoder applies its own ratio.
"""

import math

from sim.core.participant import Session, StepContext, StepResult
from sim.fabric import CanBus, CanFrame, VirtualCanDevice
from sim.physics import Rotor

from .protocol import decode_current_command, encode_feedback, validate_motor_id


class M3508(VirtualCanDevice):
    def __init__(self, bus: CanBus, motor_id: int = 1, *, rotor: Rotor | None = None,
                 torque_per_amp: float = 0.02,
                 command_timeout_ns: int = 100_000_000,
                 feedback_period_ns: int = 1_000_000,
                 temperature_c: int = 25, name: str | None = None):
        validate_motor_id(motor_id)
        if (isinstance(torque_per_amp, bool)
                or not isinstance(torque_per_amp, (int, float))
                or not math.isfinite(torque_per_amp) or torque_per_amp < 0):
            raise ValueError("torque_per_amp must be finite and nonnegative")
        for label, value in (("command_timeout_ns", command_timeout_ns),
                             ("feedback_period_ns", feedback_period_ns)):
            if type(value) is not int or not 0 < value <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError(f"{label} must be a positive unsigned64 integer")
        if type(temperature_c) is not int or not 0 <= temperature_c <= 255:
            raise ValueError("temperature_c must be an integer from 0 to 255")
        if rotor is not None and not isinstance(rotor, Rotor):
            raise TypeError("rotor must be a Rotor")
        self.motor_id = motor_id
        self.rotor = Rotor() if rotor is None else rotor
        self.torque_per_amp = torque_per_amp
        self.command_timeout_ns = command_timeout_ns
        self.feedback_period_ns = feedback_period_ns
        self.temperature_c = temperature_c
        self.closed = False
        self._session_id: int | None = None
        self._command_raw = 0
        self._command_expiry_ns = 0
        self.t_sim_ns = 0
        self._step_id = 0
        self._next_feedback_ns = 0
        self.rejected_commands = 0
        self.last_rejection: str | None = None
        super().__init__(bus, f"m3508-{motor_id}" if name is None else name)

    @property
    def current_raw(self) -> int:
        return self._command_raw if self.t_sim_ns < self._command_expiry_ns else 0

    @property
    def current_amp(self) -> float:
        return self.current_raw * (20.0 / 16384)

    def reset(self, session: Session) -> None:
        if self.closed:
            raise RuntimeError("M3508 is closed")
        if not isinstance(session, Session) or session.t_sim_ns > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("M3508 reset requires a Session with unsigned64 time")
        self.rotor.reset()
        self._session_id = session.session_id
        self.t_sim_ns = session.t_sim_ns
        self._step_id = 0
        self._command_raw = 0
        self._command_expiry_ns = session.t_sim_ns
        self._next_feedback_ns = session.t_sim_ns + self.feedback_period_ns
        self.rejected_commands = 0
        self.last_rejection = None

    def receive(self, frame: CanFrame) -> None:
        try:
            current_raw = decode_current_command(frame, self.motor_id)
        except ValueError as exc:
            self.rejected_commands += 1
            self.last_rejection = str(exc)
            return
        if current_raw is None:
            return
        if self.closed or self._session_id is None:
            raise RuntimeError("M3508 requires a successful reset")
        if frame.timestamp_ns != self.t_sim_ns:
            raise ValueError("M3508 command must be delivered at current device time")
        self._command_raw = current_raw
        self._command_expiry_ns = frame.timestamp_ns + self.command_timeout_ns

    def feedback(self) -> CanFrame:
        encoder = int((self.rotor.position % math.tau) / math.tau * 8192) % 8192
        speed_rpm = min(32767, max(-32768,
                                 round(self.rotor.velocity * 30 / math.pi)))
        return encode_feedback(self.motor_id, encoder=encoder, speed_rpm=speed_rpm,
                               current_raw=self.current_raw,
                               temperature_c=self.temperature_c,
                               timestamp_ns=self.t_sim_ns)

    def step(self, ctx: StepContext) -> StepResult:
        if self.closed or self._session_id is None:
            raise RuntimeError("M3508 requires a successful reset")
        if (not isinstance(ctx, StepContext) or ctx.session_id != self._session_id
                or ctx.step_id != self._step_id + 1
                or ctx.t_sim_ns != self.t_sim_ns + ctx.dt_ns
                or ctx.t_sim_ns > 0xFFFFFFFFFFFFFFFF):
            raise ValueError("M3508 step must follow its session and exact target")
        active_ns = (max(0, min(ctx.t_sim_ns, self._command_expiry_ns) - self.t_sim_ns)
                     if self._command_raw else 0)
        if active_ns:
            self.rotor.step(active_ns / 1_000_000_000,
                            self._command_raw * (20.0 / 16384) * self.torque_per_amp)
        coast_ns = ctx.dt_ns - active_ns
        if coast_ns:
            self.rotor.step(coast_ns / 1_000_000_000, 0.0)
        self.t_sim_ns = ctx.t_sim_ns
        self._step_id = ctx.step_id
        if self.t_sim_ns >= self._next_feedback_ns:
            self.emit(self.feedback())
            self._next_feedback_ns += (
                (self.t_sim_ns - self._next_feedback_ns) // self.feedback_period_ns + 1
            ) * self.feedback_period_ns
        return StepResult("ok", self.t_sim_ns,
                          {"current_raw": self.current_raw,
                           "rejected_commands": self.rejected_commands})

    def close(self) -> None:
        # Bus and any host transports are borrowed; their owner closes them.
        self.closed = True
