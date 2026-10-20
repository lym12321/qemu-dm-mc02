"""Native backend availability diagnostic, separate from execution failures."""


class BackendUnavailable(RuntimeError):
    """A required native binary, pinned source or capability is unavailable."""
