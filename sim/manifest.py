"""Bounded YAML admission shared by Lab, Robot and Arena manifests.

This module reads configuration files only; it opens no simulation resources.
UART text may include NUL bytes. Consumers separately admit process paths/argv.
"""

from pathlib import Path
import re

import yaml
from yaml.events import AliasEvent

MAX_FILE_BYTES = 1 << 20
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")


class ManifestError(ValueError):
    """Configuration rejected before runtime admission."""


class _BoundedLoader(yaml.SafeLoader):
    """Use PyYAML safe constructors with bounded composition and unique keys."""

    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent) or self.peek_event().anchor is not None:
            raise ManifestError("YAML anchors and aliases are not supported")
        self._nodes += 1
        if self._nodes > 40000:
            raise ManifestError("YAML node limit exceeded")
        self._depth += 1
        try:
            if self._depth > 16:
                raise ManifestError("YAML nesting limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if type(key) is not str:
            raise ManifestError("YAML mapping keys must be strings")
        if key in result:
            raise ManifestError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_BoundedLoader.add_constructor("tag:yaml.org,2002:map", _unique_mapping)


def fields(value, allowed, required, label: str) -> dict:
    """Reject unknown or absent keys in one schema mapping."""
    if type(value) is not dict:
        raise ManifestError(f"{label} must be a mapping")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown:
        raise ManifestError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ManifestError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    return value


def text_bytes(value, label: str, *, empty: bool = False,
               maximum: int = 4096) -> bytes:
    """Admit bounded UTF-8 text without interpreting its byte contents."""
    if type(value) is not str:
        raise ManifestError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ManifestError(f"{label} must be valid UTF-8") from error
    if len(encoded) > maximum or (not empty and not encoded):
        raise ManifestError(f"{label} must contain {'0' if empty else '1'}..{maximum} UTF-8 bytes")
    return encoded


def identifier(value, label: str) -> str:
    """Admit the ASCII labels shared by Robot and Arena configuration."""
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ManifestError(f"{label} must be an ASCII identifier of 1..64 characters")
    return value


def file_path(value, base: Path, label: str) -> Path:
    """Resolve bounded literal path text against its manifest directory."""
    text_bytes(value, label)
    if "\0" in value:
        raise ManifestError(f"{label} must not contain NUL")
    return readable_file(base / value, label)


def readable_file(path: Path, label: str) -> Path:
    """Resolve a regular file and verify it can be opened for reading."""
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ManifestError(f"{label} must be a regular file")
        with resolved.open("rb"):
            pass
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f"{label} cannot be opened: {error}") from error
    return resolved


def load_document(path: str | Path) -> tuple[Path, object]:
    """Return the resolved source and safely parsed bounded UTF-8 document."""
    if not isinstance(path, (str, Path)):
        raise ManifestError("manifest path must be a string or Path")
    manifest_path = readable_file(Path(path), "manifest")
    try:
        with manifest_path.open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ManifestError("manifest exceeds 1 MiB")
        # _BoundedLoader inherits only safe_load's SafeLoader constructors.
        document = yaml.load(raw.decode("utf-8"), Loader=_BoundedLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f"cannot parse manifest: {error}") from error
    return manifest_path, document
