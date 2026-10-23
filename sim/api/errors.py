"""Backend-neutral availability diagnostics, distinct from execution failures."""


class BackendUnavailable(RuntimeError):
    """A required native runtime, binary or capability is unavailable."""
