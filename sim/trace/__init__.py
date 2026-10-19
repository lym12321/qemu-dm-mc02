"""Protocol-neutral, bounded MCAP event storage, without a replay scheduler."""

from .storage import TraceEvent, TraceWriter, iter_events

__all__ = ["TraceEvent", "TraceWriter", "iter_events"]
