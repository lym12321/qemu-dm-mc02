"""C620/M3508 wire and timeout owner, independent of any physical plant.

An explicit consumer prepares a drive interval, advances its plant, and submits
the completed rotor observation. Current is commanded current, not an electrical
measurement. Rotor-to-output gearbox conversion belongs to that consumer.
"""

from dataclasses import dataclass
import math

from sim.core import Session, StepContext, StepResult
from sim.fabric import CanFrame, VirtualCanDevice

from .protocol import decode_current_command, encode_feedback, validate_motor_id


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class DrivePlan:
    context: StepContext
    active_until_ns: int
    effort_nm: float


class M3508Device(VirtualCanDevice):
    def __init__(self, bus, motor_id=1, *, torque_per_amp=.02,
                 command_timeout_ns=100_000_000, feedback_period_ns=1_000_000,
                 temperature_c=25, name=None):
        validate_motor_id(motor_id)
        try:
            torque_per_amp = _finite(torque_per_amp, "torque_per_amp")
        except ValueError as exc:
            raise ValueError("torque_per_amp must be finite and nonnegative") from exc
        if torque_per_amp < 0:
            raise ValueError("torque_per_amp must be finite and nonnegative")
        if not math.isfinite(20 * torque_per_amp):
            raise ValueError("torque_per_amp must support finite +/-20 A torque")
        for label, value in (("command_timeout_ns", command_timeout_ns),
                             ("feedback_period_ns", feedback_period_ns)):
            if type(value) is not int or not 0 < value <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError(f"{label} must be a positive unsigned64 integer")
        if type(temperature_c) is not int or not 0 <= temperature_c <= 255:
            raise ValueError("temperature_c must be an integer from 0 to 255")
        self.motor_id = motor_id
        self.torque_per_amp = torque_per_amp
        self.command_timeout_ns = command_timeout_ns
        self.feedback_period_ns = feedback_period_ns
        self.temperature_c = temperature_c
        self.closed = False
        self._session_id = None
        self._command_raw = 0
        self._command_expiry_ns = 0
        self.t_sim_ns = 0
        self._step_id = 0
        self._next_feedback_ns = 0
        self._pending = None
        self._observation = (0.0, 0.0)
        self.rejected_commands = 0
        self.last_rejection = None
        super().__init__(bus, f"m3508-{motor_id}" if name is None else name)

    @property
    def current_raw(self):
        return self._command_raw if self.t_sim_ns < self._command_expiry_ns else 0

    @property
    def current_amp(self):
        return self.current_raw * (20.0 / 16384)

    def reset(self, session: Session, *, position_rad=0.0, velocity_rad_s=0.0):
        if self.closed:
            raise RuntimeError("M3508 is closed")
        if not isinstance(session, Session) or session.t_sim_ns > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("M3508 reset requires a Session with unsigned64 time")
        observation = (_finite(position_rad, "rotor position"),
                       _finite(velocity_rad_s, "rotor velocity"))
        self._session_id = session.session_id
        self.t_sim_ns = session.t_sim_ns
        self._step_id = 0
        self._command_raw = 0
        self._command_expiry_ns = session.t_sim_ns
        self._next_feedback_ns = session.t_sim_ns + self.feedback_period_ns
        self._pending = None
        self._observation = observation
        self.rejected_commands = 0
        self.last_rejection = None

    def receive(self, frame: CanFrame):
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
        if self._pending is not None:
            raise RuntimeError("M3508 command cannot change during a prepared step")
        if frame.timestamp_ns != self.t_sim_ns:
            raise ValueError("M3508 command must be delivered at current device time")
        self._command_raw = current_raw
        self._command_expiry_ns = frame.timestamp_ns + self.command_timeout_ns

    def get_rotor_state(self) -> tuple[float, float]:
        """Latest completed rotor observation (radians, radians per second)."""
        return self._observation

    def feedback(self) -> CanFrame:
        position, velocity = self.get_rotor_state()
        position = _finite(position, "rotor position")
        velocity = _finite(velocity, "rotor velocity")
        encoder = int((position % math.tau) / math.tau * 8192) % 8192
        # Clamp before multiplication too, so a finite extreme observation
        # saturates the signed wire field without floating-point overflow.
        if velocity >= 32767 * math.pi / 30:
            speed_rpm = 32767
        elif velocity <= -32768 * math.pi / 30:
            speed_rpm = -32768
        else:
            speed_rpm = round(velocity * 30 / math.pi)
        return encode_feedback(self.motor_id, encoder=encoder, speed_rpm=speed_rpm,
                               current_raw=self.current_raw,
                               temperature_c=self.temperature_c,
                               timestamp_ns=self.t_sim_ns)

    def prepare_step(self, ctx: StepContext) -> DrivePlan:
        if self.closed or self._session_id is None:
            raise RuntimeError("M3508 requires a successful reset")
        if self._pending is not None:
            raise RuntimeError("M3508 already has a prepared step")
        if (not isinstance(ctx, StepContext) or ctx.session_id != self._session_id
                or ctx.step_id != self._step_id + 1
                or ctx.t_sim_ns != self.t_sim_ns + ctx.dt_ns
                or ctx.t_sim_ns > 0xFFFFFFFFFFFFFFFF):
            raise ValueError("M3508 step must follow its session and exact target")
        active_until = (max(self.t_sim_ns, min(ctx.t_sim_ns, self._command_expiry_ns))
                        if self._command_raw else self.t_sim_ns)
        effort = (self._command_raw * (20.0 / 16384) * self.torque_per_amp
                  if active_until > self.t_sim_ns else 0.0)
        plan = DrivePlan(ctx, active_until, _finite(effort, "rotor effort"))
        self._pending = plan
        return plan

    def complete_step(self, plan: DrivePlan, position_rad: float,
                      velocity_rad_s: float) -> StepResult:
        if self.closed or self._session_id is None:
            raise RuntimeError("M3508 requires a successful reset")
        if plan is not self._pending or plan is None:
            raise ValueError("M3508 completion requires its original prepared plan")
        observation = (_finite(position_rad, "rotor position"),
                       _finite(velocity_rad_s, "rotor velocity"))
        self._observation = observation
        self.t_sim_ns = plan.context.t_sim_ns
        self._step_id = plan.context.step_id
        self._pending = None
        # As in the original participant, emission failure is not a rollback.
        # The caller must freeze/reset; repeating completion cannot restep or
        # enqueue a duplicate physical observation.
        if self.t_sim_ns >= self._next_feedback_ns:
            self.emit(self.feedback())
            self._next_feedback_ns += (
                (self.t_sim_ns - self._next_feedback_ns) // self.feedback_period_ns + 1
            ) * self.feedback_period_ns
        return StepResult("ok", self.t_sim_ns,
                          {"current_raw": self.current_raw,
                           "rejected_commands": self.rejected_commands})

    def close(self):
        self.closed = True
        self._pending = None
