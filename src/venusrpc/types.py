"""Python wrappers around HSL values that live inside the Venus runtime.

Every wrapper holds a Python-side mirror of an HSL value plus the symbolic
name used inside the runtime. ``push()`` copies the Python value into HSL,
``pull()`` reads the current HSL value back into Python.

Names are auto-generated unless the caller supplies one. Auto-names are
guaranteed unique per process.
"""

from __future__ import annotations

import itertools
import os
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from . import _hsl
from venusrpc.connection import Connection

_name_counter = itertools.count(1)


def _autoname(prefix: str) -> str:
    return f"__vr_{prefix}_{next(_name_counter)}__"


def _parse_pos(p: Any) -> int | str:
    s = str(p)
    return int(s) if s.lstrip("-").isdigit() else s


def _hsl_literal(value: Any) -> str:
    return _hsl.literal(value)


def _infer_initial(value: Any) -> str:
    """HSL declaration initialiser for the given Python value."""
    if isinstance(value, (str, bool, int, float)):
        return f"({_hsl_literal(value)})"
    raise TypeError(f"unsupported Variable initial value: {type(value).__name__}")


class LiquidClass:
    """Reference to a Hamilton liquid class by name."""

    __slots__ = ("name", "parameters")

    def __init__(self, name: str, parameters: dict[str, Any] | None = None) -> None:
        self.name = name
        self.parameters = parameters or {}

    def __repr__(self) -> str:
        return f"LiquidClass({self.name!r})"

    def __str__(self) -> str:
        return self.name

    def __hsl_literal__(self) -> str:
        return _hsl.string_literal(self.name)


class Variable:
    """A scalar HSL variable bound to a Python value."""

    __slots__ = ("_con", "name", "_value")

    def __init__(
        self,
        con: Connection,
        value: int | float | str = 0,
        name: str | None = None,
    ) -> None:
        self._con = con
        self.name = name or _autoname("var")
        self._value = value
        con.execute(definitions=f"variable {self.name}{_infer_initial(value)};")

    @property
    def value(self) -> int | float | str:
        return self._value

    @value.setter
    def value(self, v: int | float | str) -> None:
        self._value = v

    def push(self) -> None:
        self._con.execute(f"{self.name} = {_hsl_literal(self._value)};")

    def pull(self) -> int | float | str:
        result = self._con.execute(
            f'AddJsonVariable(___JSON___, {self.name}, "value");'
        )
        self._value = result["value"]
        return self._value


class Array:
    """A 1D HSL array bound to a Python list."""

    __slots__ = ("_con", "name", "_value")

    def __init__(
        self,
        con: Connection,
        value: Iterable[Any] | None = None,
        name: str | None = None,
    ) -> None:
        self._con = con
        self.name = name or _autoname("arr")
        self._value = list(value) if value is not None else []
        con.execute(definitions=f"variable {self.name}[];")
        if self._value:
            self.push()

    @property
    def value(self) -> list[Any]:
        return self._value

    @value.setter
    def value(self, v: Iterable[Any]) -> None:
        self._value = list(v)

    def push(self) -> None:
        n = self.name
        parts = [f"{n}.SetSize(0);"]
        for item in self._value:
            parts.append(f"{n}.AddAsLast({_hsl_literal(item)});")
        self._con.execute("".join(parts))

    def pull(self) -> list[Any]:
        result = self._con.execute(f'AddJsonArray(___JSON___, {self.name}, "value");')
        self._value = list(result.get("value", []))
        return self._value


class Sequence:
    """An HSL sequence.

    ``deck_sequence=True`` means the sequence already exists in the deck layout
    and should not be redeclared. ``copy_from`` initialises from another deck
    sequence's contents.
    """

    __slots__ = ("_con", "name", "_is_deck", "labware", "position", "current", "end")

    def __init__(
        self,
        con: Connection,
        name: str | None = None,
        *,
        deck_sequence: bool = False,
        copy_from: str | None = None,
    ) -> None:
        self._con = con
        raw_name = (name.value if isinstance(name, Enum) else name) or _autoname("seq")
        self._is_deck = deck_sequence
        self.name = f"__DEVICE__.{raw_name}" if deck_sequence else raw_name
        self.labware: list[str] = []
        self.position: list[int] = []
        self.current: int = 1
        self.end: int = 0
        if not deck_sequence:
            con.execute(definitions=f"sequence {self.name};")
        if copy_from is not None:
            con.execute(f"{self.name} = {copy_from};")
            self.pull()
        elif deck_sequence:
            self.pull()

    def push(self) -> None:
        n = self.name
        if self._is_deck:
            # Deck sequences already exist in Venus; only sync traversal state.
            self._con.execute(
                f"{n}.SetCount({self.end});{n}.SetCurrentPosition({self.current});"
            )
            return
        parts = [f"{n}.SetCount(0);"]
        for lab, pos in zip(self.labware, self.position):
            parts.append(f"{n}.Add({_hsl.literal(str(lab))}, {_hsl.literal(pos)});")
        parts.append(f"{n}.SetCount({self.end});")
        parts.append(f"{n}.SetCurrentPosition({self.current});")
        self._con.execute("".join(parts))

    def pull(self) -> "Sequence":
        result = self._con.execute(
            f'AddJsonSequence(___JSON___, {self.name}, "value");'
        )
        data = result.get("value", {})
        self.labware = list(data.get("labware", []))
        self.position = [_parse_pos(p) for p in data.get("position", [])]
        self.current = int(data.get("current", 1))
        self.end = int(data.get("end", len(self.labware)))
        return self

    def pull_with_xyz(self) -> dict[str, list[Any]]:
        result = self._con.execute(
            f'AddJsonSequenceXyz(___JSON___, {self.name}, "value");'
        )
        data = result.get("value", {})
        self.labware = list(data.get("labware", []))
        self.position = [_parse_pos(p) for p in data.get("position", [])]
        self.current = int(data.get("current", 1))
        self.end = int(data.get("end", len(self.labware)))
        return data

    def as_records(self) -> list[dict[str, Any]]:
        return [
            {"labware": lab, "position": pos}
            for lab, pos in zip(self.labware, self.position)
        ]

    def add(self, labware: str, position: int | str) -> "Sequence":
        """Append a (labware, position) pair. Updates ``end`` to new length."""
        self.labware.append(str(labware))
        self.position.append(position)
        self.end = len(self.labware)
        return self

    def set_current(self, current: int) -> "Sequence":
        """Set the current-position cursor (1-based)."""
        self.current = int(current)
        return self

    def remaining(self) -> int:
        """Positions left to traverse from current cursor to end (inclusive)."""
        return max(0, self.end - self.current + 1)

    def __len__(self) -> int:
        return len(self.labware)


class Device:
    """A Venus device bound to a deck layout."""

    __slots__ = ("_con", "name", "layout_file")

    def __init__(
        self,
        con: Connection,
        layout_file: str | os.PathLike[str],
        *,
        name: str | None = None,
        main: bool = True,
        check_instrument: bool = True,
    ) -> None:
        self._con = con
        self.name = name or _autoname("dev")
        self.layout_file = Path(layout_file)
        hsl_check = "hslTrue" if check_instrument else "hslFalse"
        layout_literal = _hsl.literal(str(self.layout_file))
        con.execute(
            definitions=(
                f"device {self.name}({layout_literal}, {_hsl.literal('')}, "
                f"{hsl_check});"
            )
        )
        if main:
            con.execute(f"__DEVICE__ = {self.name};")

    @classmethod
    def from_existing(
        cls,
        con: Connection,
        name: str = "__DEVICE__",
        layout_file: str | os.PathLike[str] | None = None,
    ) -> "Device":
        """Wrap a device already declared in the running HSL host.

        Use when the host script (e.g. venusrpc_host.hsl) has already declared
        the device and a second declaration would fail the instrument check.
        ``layout_file`` defaults to the Connection's stamped layout.
        """
        if layout_file is None:
            layout_file = getattr(con, "layout_file", None) or Path()
        obj = object.__new__(cls)
        object.__setattr__(obj, "_con", con)
        object.__setattr__(obj, "name", name)
        object.__setattr__(obj, "layout_file", Path(layout_file))
        return obj
