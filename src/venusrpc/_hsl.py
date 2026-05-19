"""Small helpers for rendering Python values into HSL source."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class _HslLiteral(Protocol):
    def __hsl_literal__(self) -> str: ...


def escape_string(value: str) -> str:
    """Escape a Python string for use inside an HSL double-quoted literal."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def string_literal(value: str) -> str:
    return f'"{escape_string(value)}"'


def literal(value: Any) -> str:
    """Render a scalar Python value as an HSL literal."""
    if isinstance(value, _HslLiteral):
        return value.__hsl_literal__()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return string_literal(value)
    raise TypeError(f"unsupported HSL literal type: {type(value).__name__}")


def call(namespace: str, function: str, args: list[str]) -> str:
    return f"{namespace}::{function}({','.join(args)});"
