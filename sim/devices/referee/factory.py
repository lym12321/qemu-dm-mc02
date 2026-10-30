"""Referee fixture on the shared Simulation step and UART dispatch contracts.

The engine always evolves from reset. A true enable input starts publishing the
completed state at that input's cut, then at absolute periodic deadlines. UART
traffic in the reverse direction is ignored: this fixture models no client
requests or upload protocol. A batch event means byte-queue admission only.
"""

from sim.api import NodeDescription, PortSpec, Session, TimeQuality
from sim.api.events import check_timestamp
from sim.config.yaml import parse_time

from .config import parse_referee_configuration
from .endpoint import RefereeUartEndpoint
from .engine import RefereeEngine


def _options(options):
    values = dict(options)
    period = parse_time(values.pop("period", "100ms"), "referee period", zero=False)
    if not 1_000_000 <= period <= 60_000_000_000:
        raise ValueError("referee period must be in 1ms..60s")
    return parse_referee_configuration(values), period


class RefereeDevice:
    def __init__(self, context, configuration, period_ns):
        self._context = context
        self._engine = RefereeEngine(**configuration)
        self.period_ns = period_ns
        self._endpoint = None
        self._due = None
        self._session = None
        self._failed = None
        self._closed = self._busy = False
        self.publications = 0
        try:
            self._endpoint = RefereeUartEndpoint(context.attach("uart", self))
        except BaseException:
            self._engine.close()
            raise

    @property
    def next_due_ns(self):
        if self._failed is not None:
            raise self._failed
        return self._due

    @property
    def dispatch_budget(self):
        return 1

    @property
    def next_sequence(self):
        return self._endpoint.next_sequence

    def get_state(self):
        return self._engine.get_state()

    def _check(self):
        if self._closed or self._session is None:
            raise RuntimeError("referee device requires a successful reset")
        if self._failed is not None:
            raise self._failed
        if self._busy:
            raise self._freeze(RuntimeError("referee device cannot be reentered"))

    def _freeze(self, error):
        if self._failed is None:
            self._failed = error
        return self._failed

    def reset(self, session):
        if self._closed or self._busy:
            raise RuntimeError("referee device cannot reset while closed or executing")
        if not isinstance(session, Session):
            raise TypeError("referee reset requires a Session")
        self._busy = True
        try:
            self._engine.reset(session)
            self._endpoint.reset()
            self._session = session
            self._due = None
            self.publications = 0
            self._failed = None
        except BaseException as error:
            raise self._freeze(error)
        finally:
            self._busy = False

    def step(self, context):
        self._check()
        self._busy = True
        try:
            return self._engine.step(context)
        except BaseException as error:
            raise self._freeze(error)
        finally:
            self._busy = False

    def input(self, port, payload):
        self._check()
        if port != "enable" or payload is not True:
            raise ValueError("referee enable requires true")
        self._context.check_io()
        if self._due is None:
            self._due = self._context.t_sim_ns

    def receive_uart(self, data):
        self._check()
        if type(data) is not bytes:
            raise TypeError("referee UART input requires bytes")
        # No referee uplink parser is provided. Do not echo or queue uploads.

    def dispatch(self, cut, budget):
        self._check()
        check_timestamp(cut)
        if type(budget) is not int or budget < 0:
            raise ValueError("referee dispatch budget must be a nonnegative integer")
        if self._due is None or self._due > cut or budget == 0:
            return 0
        if self._engine.t_sim_ns != cut:
            raise RuntimeError("referee publication requires the completed engine state at its cut")
        self._busy = True
        try:
            next_due = self._due + self.period_ns
            check_timestamp(next_due)
            if next_due <= cut:
                raise RuntimeError("referee publication deadline was missed")
            first_sequence = self._endpoint.next_sequence
            due = self._due
            wire = self._endpoint.send_state(self._engine.get_state())
            self._due = next_due
            self.publications += 1
            if self._failed is not None:
                raise self._failed
            self._context.emit("uart", "referee.batch.admitted", {
                "data": wire, "first_sequence": first_sequence,
                "next_sequence": self._endpoint.next_sequence,
                "publication": self.publications, "period_ns": self.period_ns, "due_ns": due,
                "next_due_ns": next_due,
            }, source_time_ns=cut, mapped_time_ns=cut, delivery_time_ns=cut,
               quality=TimeQuality.EXACT)
            if self._failed is not None:
                raise self._failed
            return 1
        except BaseException as error:
            # net.uart BufferError freezes its owner. Never retry a batch here.
            raise self._freeze(error)
        finally:
            self._busy = False

    def close(self):
        if self._busy:
            raise RuntimeError("referee device cannot close during execution")
        if self._closed:
            return
        self._closed = True
        primary = None
        for owner in (self._endpoint, self._engine):
            try:
                owner.close()
            except BaseException as error:
                if primary is None:
                    primary = error
                elif error is not primary:
                    primary.add_note(f"referee owner cleanup failed: {type(error).__name__}: {error}")
        if primary is not None:
            raise primary


class RefereeFactory:
    @staticmethod
    def describe(spec):
        _options(spec.options)
        return NodeDescription((
            PortSpec("uart", "uart", min_connections=1),
            PortSpec("enable", "referee-enable", "in", "signal",
                     capabilities={"external_input"}),
        ), frozenset({"step", "dispatch", "input"}))

    @staticmethod
    def validate_input(spec, port, payload, *, connected_ports):
        RefereeFactory.describe(spec).port(port)
        if port != "enable" or payload is not True:
            raise ValueError("referee enable requires true")
        return True

    @classmethod
    def validate_topology(cls, topology, descriptions, registry):
        names = {node.name for node in topology.nodes if isinstance(registry[node.kind], cls)}
        if any(endpoint.node in names and endpoint.port == "enable"
               for link in topology.links for endpoint in (link.source, link.target)):
            raise ValueError("referee enable accepts external input only")

    @staticmethod
    def create(spec, context):
        configuration, period = _options(spec.options)
        return RefereeDevice(context, configuration, period)
