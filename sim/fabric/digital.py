"""Sampled digital wiring over completed states, without an electrical solver.

One driver feeds bounded sinks in attachment order. An unavailable initial
source leaves inputs untouched; losing output drive after activation fails.
Sampling and delivery are separate phases, so sink writes never cause another
source read at the same cut. Native source time is observation metadata, not an
edge timestamp or a firmware-consumption receipt.
"""

from dataclasses import dataclass

from sim.api.events import check_timestamp
from sim.api.topology import immutable

from ._sampled import _SampledFanout, _SampledNetFactory


@dataclass(frozen=True)
class DigitalSample:
    level: int | None
    source_time_ns: int
    mapped_time_ns: int

    def __post_init__(self):
        if self.level is not None and (type(self.level) is not int or self.level not in (0, 1)):
            raise ValueError('digital level must be integer 0, 1 or None')
        check_timestamp(self.source_time_ns)
        check_timestamp(self.mapped_time_ns)


class DigitalNet(_SampledFanout):
    """Own sampled broadcasts while borrowing every endpoint and its context."""

    _protocol = 'digital'

    def _reset_sample_state(self):
        self._level = None
        self._active = False

    def _validate_sample(self, sample, cut):
        if not isinstance(sample, DigitalSample):
            raise TypeError(f'digital source {self._driver[0]} must return a DigitalSample')
        if sample.mapped_time_ns > cut:
            raise ValueError(f'digital source {self._driver[0]} sample is beyond the completed cut')
        if self._sample is not None and (
                sample.mapped_time_ns < self._sample.mapped_time_ns
                or sample.source_time_ns < self._sample.source_time_ns):
            raise ValueError(f'digital source {self._driver[0]} sample time must not decrease')
        if self._active and sample.level is None:
            raise RuntimeError(f'digital source {self._driver[0]} lost output drive after activation; '
                               'release is unsupported')
        return sample.level is not None and sample.level != self._level

    def _commit_sample(self, sample):
        self._level = sample.level
        self._active = sample.level is not None

    def _sample_payload(self, sample, changed):
        return {'level': sample.level, 'active': self._active, 'changed': changed}

    def _delivery_value(self, sample):
        return sample.level

    def _delivery_payload(self, sample):
        return {'level': sample.level}

    def observe(self):
        return immutable({'active': self._active, 'level': self._level,
                          'sample_time_ns': self._sample_cut, 'pending': self.pending,
                          'failed': self._failed is not None})


class DigitalNetFactory(_SampledNetFactory):
    _protocol = 'digital'
    _net_type = DigitalNet
