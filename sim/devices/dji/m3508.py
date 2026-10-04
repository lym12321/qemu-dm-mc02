"""Virtual M3508 using the C620 CAN subset and a separate simple rotor.

The default J=.02 kg m², damping=.08 Nm/(rad/s), .02 Nm/A torque coefficient,
25 Celsius and 100 ms command timeout are uncalibrated model assumptions.
Feedback is emitted at most once per explicit step: missed 1 kHz samples are
coalesced into the latest state. Encoder uses floor after rotor wrap; RPM uses
nearest integer (ties to even) and saturates to signed16. Current feedback is
the instantaneous commanded current, not an electrical measurement. There is
no gearbox conversion; the real firmware decoder applies its own ratio.
"""

from sim.core import Session, StepContext, StepResult
from sim.fabric import CanBus
from sim.physics import Rotor

from .device import M3508Device


class M3508(M3508Device):
    """Compatibility participant: the shared protocol over one simple rotor."""

    def __init__(self, bus: CanBus, motor_id: int = 1, *, rotor: Rotor | None = None,
                 torque_per_amp: float = 0.02,
                 command_timeout_ns: int = 100_000_000,
                 feedback_period_ns: int = 1_000_000,
                 temperature_c: int = 25, name: str | None = None):
        if rotor is not None and not isinstance(rotor, Rotor):
            raise TypeError("rotor must be a Rotor")
        self.rotor = Rotor() if rotor is None else rotor
        super().__init__(bus, motor_id, torque_per_amp=torque_per_amp,
                         command_timeout_ns=command_timeout_ns,
                         feedback_period_ns=feedback_period_ns,
                         temperature_c=temperature_c, name=name)

    def reset(self, session: Session) -> None:
        super().reset(session, position_rad=self.rotor.initial_position,
                      velocity_rad_s=self.rotor.initial_velocity)
        self.rotor.reset()

    def get_rotor_state(self) -> tuple[float, float]:
        # Preserve legacy live feedback, including a caller-supplied Rotor.
        return self.rotor.position, self.rotor.velocity

    def step(self, ctx: StepContext) -> StepResult:
        plan = self.prepare_step(ctx)
        start_ns = ctx.t_sim_ns - ctx.dt_ns
        active_ns = plan.active_until_ns - start_ns
        if active_ns:
            self.rotor.step(active_ns / 1_000_000_000, plan.effort_nm)
        coast_ns = ctx.t_sim_ns - plan.active_until_ns
        if coast_ns:
            self.rotor.step(coast_ns / 1_000_000_000, 0.0)
        return self.complete_step(plan, self.rotor.position, self.rotor.velocity)
