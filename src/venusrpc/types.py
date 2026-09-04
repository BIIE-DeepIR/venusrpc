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

from . import _hsl, step_data
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

    __slots__ = (
        "_con", "name", "_is_deck", "labware", "position", "current", "end",
        "_pushed",
    )

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
        # (labware, position) pairs already materialised in the HSL sequence, so
        # re-pushes only sync the cursor instead of re-Adding (see push()).
        self._pushed: list[tuple[str, int | str]] = []
        if not deck_sequence:
            con.execute(definitions=f"sequence {self.name};")
        if copy_from is not None:
            con.execute(f"{self.name} = {copy_from};")
            self.pull_full()
        elif deck_sequence:
            self.pull_full()

    def push(self) -> None:
        n = self.name
        if self._is_deck:
            # Deck sequences already exist in Venus; only sync traversal state.
            self._con.execute(
                f"{n}.SetCount({self.end});{n}.SetCurrentPosition({self.current});"
            )
            return
        # The generated SMT wrapper re-push()es every sequence arg before every
        # call. HSL's Sequence.Add appends to GetTotal() even after SetCount(0),
        # so re-Adding the whole content each call grows the underlying storage
        # without bound — and AddJsonSequence dumps GetTotal(), producing the
        # runaway labware payload. Materialise contents once, then only sync the
        # cursor; Add just the genuinely-new trailing entries.
        current_pairs = list(zip(self.labware, self.position))
        if self._pushed == current_pairs:
            self._con.execute(
                f"{n}.SetCount({self.end});{n}.SetCurrentPosition({self.current});"
            )
            return
        parts: list[str] = []
        if current_pairs[: len(self._pushed)] == self._pushed:
            start = len(self._pushed)  # append-only: add only the new tail
        else:
            # ponytail: content diverged (rare — callers build-then-traverse).
            # SetCount(0) can't truncate GetTotal, so this path re-grows storage;
            # acceptable since it isn't the hot path. Make a fresh Sequence to
            # fully reset if a caller ever rebuilds contents in a loop.
            parts.append(f"{n}.SetCount(0);")
            start = 0
        for lab, pos in current_pairs[start:]:
            # HSL sequence.Add types both args as strings; a bare numeric
            # position trips "type mismatch in formal parameter 2". pull()'s
            # _parse_pos turns digit-only positions back into ints, so always
            # stringify here to survive a push/pull/push round-trip.
            parts.append(f"{n}.Add({_hsl.literal(str(lab))}, {_hsl.literal(str(pos))});")
        parts.append(f"{n}.SetCount({self.end});")
        parts.append(f"{n}.SetCurrentPosition({self.current});")
        self._con.execute("".join(parts))
        self._pushed = current_pairs

    def pull(self) -> "Sequence":
        """Refresh the cursor (current, end) only.

        Labware/position are set by ``push()`` and HSL never mutates them, so
        round-tripping the arrays per SMT call wastes ~40 bytes/entry and
        exposes an HSL ``sequence`` storage-doubling quirk (each ``push()``
        appends without truncating ``GetTotal()``). Cursor-only keeps the
        result bounded regardless. Use ``pull_full()`` if you really need the
        contents (e.g. inspecting a sequence Venus populated for you).
        """
        result = self._con.execute(
            f'AddJsonVariable(___JSON___, {self.name}.GetCurrentPosition(), "current");'
            f'AddJsonVariable(___JSON___, {self.name}.GetCount(), "end");'
        )
        self.current = int(result.get("current", self.current))
        self.end = int(result.get("end", self.end))
        return self

    def pull_full(self) -> "Sequence":
        """Pull cursor + labware/position. Use at init or when Venus mutated content."""
        result = self._con.execute(
            f'AddJsonSequence(___JSON___, {self.name}, "value");'
        )
        data = result.get("value", {})
        self.labware = list(data.get("labware", []))
        self.position = [_parse_pos(p) for p in data.get("position", [])]
        self.current = int(data.get("current", 1))
        self.end = int(data.get("end", len(self.labware)))
        # HSL already holds these entries; record them (normalised) so the next
        # push() syncs the cursor instead of re-Adding and doubling them.
        self._pushed = list(zip(self.labware, self.position))
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
        self._pushed = list(zip(self.labware, self.position))
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


# Instrument identity and the front cover both come out of Hamilton's own
# libraries, resolved against Venus's default Library\ path: the serial from an
# HSL function, the cover from a device step.
_SERIAL_INCLUDE = '#include "HSLGetSerialNumber.hsl"'
_MAINT_LIB_STP = "HslStarLineMaintMetLib.stp"

# One step does both directions -- its LockFrontCoverState is 0 in the first
# block and 1 in the second. Calling it by guid is what MtcLib::LockCover does
# internally, minus the MessageBox loops wrapped around it there.
_COVER_STEP = "_2931977C_E32F_4471_8B68_F31ED1A866F5"
_COVER_UNLOCK = "13358f08_1f67_4dcd_ac2dd6c120e15c21"
_COVER_LOCK = "ca965d5b_e567_47e5_a8d23f2768eee8ec"


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

    # ── Instrument identity ─────────────────────────────────────────────────

    def serial_number(self) -> str:
        """The instrument's serial, e.g. ``"SN2043"``.

        Needs an initialized instrument -- the firmware call behind it (C0RI)
        answers nothing before that. Venus simulation answers a simulator
        serial instead of the real one: SN1234 on the Vantage / STAR-V line,
        SN0000 on the STAR line.
        """
        res = self._con.execute(
            f'AddJsonVariable(___JSON___, GetSerialNumber({self.name}), "sn");',
            definitions=_SERIAL_INCLUDE,
        )
        return str(res.get("sn", ""))

    # ── Front cover ─────────────────────────────────────────────────────────

    def open_front_cover(self) -> None:
        """Lower the motorised front cover so an operator can reach the deck.

        Vantage-specific geometry: the cover is hinged along the bottom, so it
        flips DOWN to open and rises to close. It is held shut by its motor,
        not a latch (C0AMQW answers ``cg0 0 0 0 0 0`` -- no lock fitted), which
        is why releasing the lock is what drops it.
        """
        self._front_cover(_COVER_UNLOCK)

    def close_front_cover(self) -> None:
        """Raise the front cover shut again.

        Venus raises it anyway when the method ends, so a run that opened the
        cover does not have to put it back -- this is for closing it *during* a
        run, between two operator steps.
        """
        self._front_cover(_COVER_LOCK)

    def _front_cover(self, guid: str) -> None:
        """Call the cover step directly, bypassing ``MtcLib::LockCover``.

        The library function is unusable in both directions: its lock branch
        loops ``while (!IsCoverClosed) MessageBox(...)`` and this instrument's
        cover status is hardwired to "open", so it prompts forever; and its
        unlock branch prompts too unless the message and title are both empty.
        The step underneath has no such wrapper.
        """
        self._con.execute(
            f'{self.name}.{_COVER_STEP}("{guid}");',
            step_data=step_data.read(guid, _MAINT_LIB_STP),
        )


def _interp_width(
    segments: tuple[tuple[float | None, float | None, float | None], ...],
    height_from_base: float,
) -> float | None:
    """Footprint width (mm) at a height above the base, from a segment profile.

    ``segments`` is a bottom-up tuple of ``(lower_width, upper_width,
    segment_height)``. Returns ``None`` if no profile is available.
    """
    if not segments:
        return None
    z = 0.0
    for lower, upper, seg_h in segments:
        h = seg_h or 0.0
        lo = lower if lower is not None else upper
        up = upper if upper is not None else lower
        if height_from_base <= z + h or h == 0.0:
            if lo is None:
                return up
            if up is None:
                return lo
            frac = 0.0 if h == 0 else max(0.0, min(1.0, (height_from_base - z) / h))
            return lo + (up - lo) * frac
        z = z + h
    last = segments[-1]
    return last[1] if last[1] is not None else last[0]


class LabwareKind(str, Enum):
    """The kind of a labware definition, from its source-file extension."""

    RACK = "rck"  # .rck — rectangular rack / plate / tip or tube carrier base
    CARRIER = "tml"  # .tml — carrier template
    LID = "lid"  # .lid — plate lid / cover


class LabwareDef:
    """Common base for generated labware definitions.

    Concrete definitions are emitted by :meth:`venusrpc.Codegen.read_labware`
    into ``__labware__.py``; each subclasses the kind-specific base below
    (:class:`Rack`, :class:`Carrier`, :class:`Lid`) and sets class attributes
    like ``length``/``width``/``height`` and the footprint profile.
    """

    kind: "LabwareKind"
    model: str = ""  # the definition / labware-type name (e.g. "Cos_96_HalfArea")
    file: str = ""
    length: float | None = None
    width: float | None = None
    height: float | None = None
    stack_height: float | None = None
    segments_x: tuple[tuple[float | None, float | None, float | None], ...] = ()
    segments_y: tuple[tuple[float | None, float | None, float | None], ...] = ()

    @classmethod
    def width_at_x(cls, height_from_base: float) -> float | None:
        """Footprint X width (mm) at the given height above the base."""
        return _interp_width(cls.segments_x, height_from_base)

    @classmethod
    def width_at_y(cls, height_from_base: float) -> float | None:
        """Footprint Y width (mm) at the given height above the base."""
        return _interp_width(cls.segments_y, height_from_base)


class Rack(LabwareDef):
    """A rectangular rack — plate, tip rack, or tube-carrier base (``.rck``)."""

    kind = LabwareKind.RACK
    rows: int | None = None
    columns: int | None = None
    pitch_x: float | None = None
    pitch_y: float | None = None
    well_depth: float | None = None
    well_max_depth: float | None = None
    well_max_volume: float | None = None
    volume_eqn: str | None = None


class Carrier(LabwareDef):
    """A carrier template that holds racks/plates on sites (``.tml``)."""

    kind = LabwareKind.CARRIER


class Lid(LabwareDef):
    """A plate lid / cover (``.lid``)."""

    kind = LabwareKind.LID


class LabwareInstance:
    """A labware placed on a deck — its exact deck name plus its definition.

    Generated onto ``<Deck>.Instances``. The ``name`` is the exact labware Id
    as placed in the layout (what you reference on the deck); ``definition`` is
    the shared :class:`LabwareDef` subclass. Any other attribute access proxies
    to the definition, so ``inst.length`` / ``inst.kind`` / ``inst.model`` work
    directly without duplicating the definition's data.
    """

    __slots__ = ("name", "definition")

    def __init__(self, definition: type[LabwareDef], name: str | None = None) -> None:
        self.definition = definition
        self.name = name  # filled from the attribute name by __set_name__ if None

    def __set_name__(self, owner: type, attr: str) -> None:
        if self.name is None:
            self.name = attr

    def __getattr__(self, item: str) -> object:
        # name/definition are real slots; everything else comes from the def.
        if item in ("name", "definition"):
            raise AttributeError(item)
        return getattr(self.definition, item)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"LabwareInstance({self.name!r} -> {self.definition.__name__})"
