"""Completed site-local SI IMU broadcasts over a bounded signal connection.

Every completed cut is delivered, including unchanged measurements. Source
timestamps, when available, are observation metadata; they do not establish a
sensor sample identity, electrical timing or firmware consumption.
"""

from sim.api import ImuSignalSample, validate_imu_sample
from sim.api.events import check_timestamp
from sim.api.topology import immutable

from ._sampled import _SampledFanout, _SampledNetFactory


class ImuNet(_SampledFanout):
    """Borrow one pull source and ordered sinks; never advance their state."""

    _protocol = 'imu'

    def _reset_sample_state(self):
        self._last_source_time_ns = None

    def _validate_sample(self, sample, cut):
        if not isinstance(sample, ImuSignalSample):
            raise TypeError(f'imu source {self._driver[0]} must return an ImuSignalSample')
        validate_imu_sample(sample.sample)
        check_timestamp(sample.mapped_time_ns)
        if sample.source_time_ns is not None:
            check_timestamp(sample.source_time_ns)
        if sample.mapped_time_ns != cut:
            raise ValueError(f'imu source {self._driver[0]} sample must match the completed cut')
        if (sample.source_time_ns is not None and self._last_source_time_ns is not None
                and sample.source_time_ns < self._last_source_time_ns):
            raise ValueError(f'imu source {self._driver[0]} sample time must not decrease')
        return True

    def _commit_sample(self, sample):
        if sample.source_time_ns is not None:
            self._last_source_time_ns = sample.source_time_ns

    def _sample_payload(self, sample, _enqueue):
        return {'sample': sample}

    def _delivery_value(self, sample):
        return sample

    def _delivery_payload(self, sample):
        return {'sample': sample}

    def observe(self):
        return immutable({'sample': self._sample, 'sample_time_ns': self._sample_cut,
                          'pending': self.pending, 'failed': self._failed is not None})


class ImuNetFactory(_SampledNetFactory):
    _protocol = 'imu'
    _net_type = ImuNet
