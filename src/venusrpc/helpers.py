"""Static utilities for well plate addressing.

No Venus runtime needed. Pure math/string functions used by both runtime code
and codegen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PlateType = Literal[6, 12, 24, 48, 96, 384, 1536]
SortingMode = Literal["row", "column"]

_LAYOUT: dict[int, tuple[int, int]] = {
    6: (2, 3),
    12: (3, 4),
    24: (4, 6),
    48: (6, 8),
    96: (8, 12),
    384: (16, 24),
    1536: (32, 48),
}


@dataclass(frozen=True)
class PlateShape:
    rows: int
    columns: int

    @property
    def well_count(self) -> int:
        return self.rows * self.columns

    @classmethod
    def for_plate(cls, plate_type: PlateType) -> "PlateShape":
        try:
            rows, cols = _LAYOUT[plate_type]
        except KeyError as exc:
            raise ValueError(
                f"unsupported plate_type {plate_type}; "
                f"valid: {sorted(_LAYOUT)}"
            ) from exc
        return cls(rows=rows, columns=cols)


def _row_letter(row_index: int) -> str:
    if row_index < 26:
        return chr(ord("A") + row_index)
    # Beyond Z (e.g. 1536-well plates): AA, AB, ...
    first = row_index // 26 - 1
    second = row_index % 26
    return chr(ord("A") + first) + chr(ord("A") + second)


class Helpers:
    """Plate / well addressing utilities. All methods are static."""

    @staticmethod
    def numeric_to_alphanumeric(
        well_number: int,
        plate_type: PlateType = 96,
        sorting_mode: SortingMode = "column",
        zero_padding: bool = False,
    ) -> str:
        """Convert 1-based well number to e.g. ``A1`` / ``A01``."""
        shape = PlateShape.for_plate(plate_type)
        if not 1 <= well_number <= shape.well_count:
            raise ValueError(
                f"well_number {well_number} out of range "
                f"for {plate_type}-well plate (1..{shape.well_count})"
            )
        idx = well_number - 1
        if sorting_mode == "column":
            row = idx % shape.rows
            col = idx // shape.rows
        elif sorting_mode == "row":
            row = idx // shape.columns
            col = idx % shape.columns
        else:
            raise ValueError(f"sorting_mode must be 'row' or 'column'")
        col_str = f"{col + 1:02d}" if zero_padding else str(col + 1)
        return f"{_row_letter(row)}{col_str}"

    @staticmethod
    def get_well_map(
        plate_type: PlateType = 96,
        sorting_mode: SortingMode = "column",
        zero_padding: bool = False,
    ) -> list[str]:
        shape = PlateShape.for_plate(plate_type)
        return [
            Helpers.numeric_to_alphanumeric(
                n, plate_type, sorting_mode, zero_padding
            )
            for n in range(1, shape.well_count + 1)
        ]

    @staticmethod
    def alphanumeric_to_numeric(
        well_id: str,
        plate_type: PlateType = 96,
        sorting_mode: SortingMode = "column",
    ) -> int:
        shape = PlateShape.for_plate(plate_type)
        letters = "".join(c for c in well_id if c.isalpha())
        digits = "".join(c for c in well_id if c.isdigit())
        if not letters or not digits:
            raise ValueError(f"invalid well id: {well_id!r}")
        letters = letters.upper()
        if len(letters) == 1:
            row = ord(letters) - ord("A")
        else:
            row = (ord(letters[0]) - ord("A") + 1) * 26 + (ord(letters[1]) - ord("A"))
        col = int(digits) - 1
        if not (0 <= row < shape.rows and 0 <= col < shape.columns):
            raise ValueError(
                f"well {well_id!r} out of range for {plate_type}-well plate"
            )
        if sorting_mode == "column":
            return col * shape.rows + row + 1
        return row * shape.columns + col + 1
