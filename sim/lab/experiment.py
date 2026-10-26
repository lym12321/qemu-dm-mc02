"""Consume actual Lab observations without owning IO or clocks.

The native Lab adapter supplies QEMU times; Simulation supplies public Event
times. UART assertions use a post-read QMP upper bound; CAN assertions retain
the wire time supplied by LabChannels. The consumer stores only assertion
status and the minimum UART pattern suffix, not a full transcript or a shadow
of board state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from sim.boards import digital_level
from sim.fabric import CanFrame
from sim.fabric.can import check_can_time

from .manifest import Assertion, LabManifest

if TYPE_CHECKING:
    from sim.boards.channels import ChannelEvent


@dataclass(frozen=True)
class AssertionObservation:
    """Actual bytes/frame and their admitted assertion timestamp."""

    endpoint: str
    timestamp_ns: int
    data: bytes | CanFrame


class AssertionEvaluator:
    """Pure shared strict-before matcher; no input dispatch, IO or clock owner."""

    def __init__(self, assertions: tuple[Assertion, ...], duration_ns: int, origin_ns: int):
        check_can_time(origin_ns)
        check_can_time(duration_ns)
        check_can_time(origin_ns + duration_ns)
        if (type(assertions) is not tuple or any(type(item) is not Assertion for item in assertions)
                or any(item.kind not in ("uart_contains", "digital_equals", "can_seen")
                       for item in assertions)):
            raise ValueError("unsupported typed assertions")
        self.assertions, self.duration_ns = assertions, duration_ns
        self.origin_ns = self._time_ns = origin_ns
        self._status = ["pending"] * len(assertions)
        self._tails = [b""] * len(assertions)
        self._finished = False

    @property
    def status(self) -> tuple[str, ...]:
        return tuple(self._status)

    @property
    def all_passed(self) -> bool:
        return all(status == "pass" for status in self._status)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"assertion[{index + 1}] {assertion.kind} {assertion.endpoint} "
            f"did not match before {assertion.before_ns} ns"
            for index, assertion in enumerate(self.assertions)
            if self._status[index] == "fail")

    def _check_clock(self, timestamp_ns: int) -> None:
        check_can_time(timestamp_ns, self._time_ns)

    def _expire(self, timestamp_ns: int) -> None:
        for index, assertion in enumerate(self.assertions):
            if (self._status[index] == "pending" and
                    timestamp_ns - self.origin_ns >= assertion.before_ns):
                self._status[index] = "fail"
                self._tails[index] = b""

    def observe_values(self, digital, events, uart_bound_ns: int, *, expire=True):
        """Consume actual projections; optionally defer expiry until a cut ends.

        Deferred expiry permits streaming same-cut batches without retaining a
        transcript. The consumer must close each completed cut with advance().
        """
        self._check_clock(uart_bound_ns)
        if self._finished:
            raise RuntimeError("Lab experiment is finished")
        for endpoint, value in digital.items():
            if not isinstance(endpoint, str) or type(value) is not int or value not in (0, 1):
                raise ValueError("digital observations require endpoint and integer level")
        for event in events:
            if not isinstance(event, AssertionObservation):
                raise TypeError("assertion observations require AssertionObservation")
            check_can_time(event.timestamp_ns)
            if event.timestamp_ns > uart_bound_ns:
                raise ValueError("observation is later than its completed bound")
            if not isinstance(event.endpoint, str) or not event.endpoint:
                raise ValueError("observation endpoint must be nonempty")
            if not isinstance(event.data, (bytes, CanFrame)):
                raise TypeError("observation data must be bytes or CanFrame")
        elapsed_ns = uart_bound_ns - self.origin_ns
        for index, assertion in enumerate(self.assertions):
            if self._status[index] != "pending":
                continue
            matched = False
            if assertion.kind == "digital_equals":
                matched = (elapsed_ns < assertion.before_ns and
                           digital.get(assertion.endpoint) == assertion.value)
            else:
                for event in events:
                    if event.endpoint != assertion.endpoint:
                        continue
                    if (assertion.kind == "uart_contains" and
                            isinstance(event.data, bytes) and
                            self.origin_ns <= event.timestamp_ns <
                            self.origin_ns + assertion.before_ns):
                        combined = self._tails[index] + event.data
                        matched = assertion.value in combined
                        keep = len(assertion.value) - 1
                        self._tails[index] = combined[-keep:] if keep else b""
                    elif (assertion.kind == "can_seen" and
                          isinstance(event.data, CanFrame) and
                          self.origin_ns <= event.timestamp_ns <
                          self.origin_ns + assertion.before_ns):
                        frame = event.data
                        wanted = assertion.value
                        matched = (
                            frame.can_id == wanted.can_id and
                            (wanted.flags is None or frame.flags == wanted.flags) and
                            (wanted.dlc is None or frame.dlc == wanted.dlc) and
                            (wanted.data is None or frame.data == wanted.data))
                    if matched:
                        break
            if matched:
                self._status[index] = "pass"
                self._tails[index] = b""
        if expire:
            self._expire(uart_bound_ns)
        self._time_ns = uart_bound_ns

    def advance(self, timestamp_ns: int):
        """Expire a completed observation cut, including one without events."""
        self._check_clock(timestamp_ns)
        if self._finished:
            raise RuntimeError("Lab experiment is finished")
        self._expire(timestamp_ns)
        self._time_ns = timestamp_ns

    def finish(self, timestamp_ns: int) -> tuple[str, ...]:
        """Finalize assertion status at or after the configured duration."""
        self._check_clock(timestamp_ns)
        if timestamp_ns < self.origin_ns + self.duration_ns:
            raise ValueError("Lab experiment cannot finish before its duration")
        self._expire(timestamp_ns)
        self._time_ns = timestamp_ns
        self._finished = True
        return self.status


class LabExperiment(AssertionEvaluator):
    """Pure host assertion consumer for an already validated LabManifest.

    ``origin_ns`` comes from the runner's actual start/ready observation.
    Stimulus times and strict assertion deadlines are relative to that origin.
    Only an observation can pass an assertion. At each observation, matching
    comes before deadline expiry, permitting a still-pending CAN assertion to
    consume a late-arriving frame whose original wire time precedes its deadline.
    Once passed or failed, an assertion never changes again.

    This adapter only projects native snapshots/channel batches for existing
    Scenario/replay consumers. Finite input scheduling belongs to Simulation.
    """

    def __init__(self, manifest: LabManifest, origin_ns: int):
        if not isinstance(manifest, LabManifest):
            raise TypeError("LabExperiment requires a validated LabManifest")
        super().__init__(manifest.assertions, manifest.duration_ns, origin_ns)
        self.manifest = manifest

    def observe(self, snapshot: dict, events: list[ChannelEvent],
                uart_bound_ns: int) -> None:
        """Match actual digital/byte/frame observations, then expire deadlines."""
        from sim.boards.channels import ChannelEvent

        self._check_clock(uart_bound_ns)
        if self._finished:
            raise RuntimeError("Lab experiment is finished")
        if not isinstance(snapshot, dict):
            raise TypeError("Lab snapshot must be an object")
        timestamp_ns = snapshot.get("timestamp_ns")
        check_can_time(timestamp_ns)
        if timestamp_ns != uart_bound_ns:
            raise ValueError("UART upper bound must be the post-read snapshot time")
        if not isinstance(events, list):
            raise TypeError("Lab events must be a list")
        for event in events:
            if not isinstance(event, ChannelEvent):
                raise TypeError("Lab observations require ChannelEvent values")
            if not isinstance(event.endpoint, str) or not event.endpoint:
                raise ValueError("Lab event endpoint must be a nonempty string")
            check_can_time(event.timestamp_ns)
            if event.timestamp_ns > uart_bound_ns:
                raise ValueError("channel observation is later than the QEMU bound")
            if not isinstance(event.data, (bytes, CanFrame)):
                raise TypeError("Lab channel data must be bytes or a CanFrame")
            if (isinstance(event.data, CanFrame) and
                    event.timestamp_ns != event.data.timestamp_ns):
                raise ValueError("CAN event and original wire timestamp disagree")

        # Validate every digital projection before changing any assertion state.
        digital = {
            assertion.endpoint: digital_level(assertion.endpoint, snapshot)
            for index, assertion in enumerate(self.manifest.assertions)
            if assertion.kind == "digital_equals"
        }
        self.observe_values(digital, tuple(AssertionObservation(
            event.endpoint, uart_bound_ns if isinstance(event.data, bytes) else event.timestamp_ns,
            event.data) for event in events), uart_bound_ns)
