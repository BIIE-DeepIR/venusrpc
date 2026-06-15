"""venusrpc — HTTP-based Python bridge to the Hamilton Venus HSL runtime."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Literal

from venusrpc.connection import Connection, VenusError, VenusTransportError
from venusrpc.types import (
    Array,
    Carrier,
    Device,
    LabwareDef,
    LabwareInstance,
    LabwareKind,
    Lid,
    LiquidClass,
    Rack,
    Sequence,
    Variable,
)
from venusrpc.helpers import Helpers
from venusrpc.labware import Labware, LabwarePlacementError
from venusrpc.codegen import Codegen
from venusrpc.simulation import (
    SimulationState,
    get_simulation,
    set_simulation,
)

__all__ = [
    "Connection",
    "VenusError",
    "VenusTransportError",
    "Variable",
    "Array",
    "Sequence",
    "Device",
    "LabwareDef",
    "LabwareInstance",
    "LabwareKind",
    "Rack",
    "Carrier",
    "Lid",
    "LiquidClass",
    "Helpers",
    "Labware",
    "LabwarePlacementError",
    "Codegen",
    "SimulationState",
    "get_simulation",
    "set_simulation",
    "verify_resources",
]

__version__ = "0.1.0"


class StaleResourcesError(RuntimeError):
    """Raised when generated wrappers are out of sync with their sources."""


def verify_resources(
    config: str | Path | None = None,
    *,
    mode: Literal["strict", "warn", "auto"] = "auto",
) -> list[tuple[Path, str]]:
    """Opt-in check that generated wrappers match their source hashes.

    Reads ``[tool.venusrpc]`` from pyproject.toml / venusrpc.toml (or the
    file passed in ``config``). Returns the list of stale entries.

    ``mode``:
      * ``"strict"`` — raise :class:`StaleResourcesError` if any entry stale.
      * ``"warn"``   — emit a :class:`UserWarning` per stale entry.
      * ``"auto"``   — read ``verify_on_import`` from the config; accepts
        ``true`` (strict), ``"warn"`` (warn), ``false`` (no-op).
    """
    from venusrpc.cli import _resolve_config

    cfg, base = _resolve_config(str(config) if config else None)

    if mode == "auto":
        flag = cfg.get("verify_on_import", False)
        if flag is False:
            return []
        mode = "warn" if str(flag).lower() == "warn" else "strict"

    output_dir = cfg.get("output_dir")
    output_path = (base / output_dir).resolve() if output_dir else None
    layouts = cfg.get("layouts", [])
    if isinstance(layouts, str):
        layouts = [layouts]
    layout_paths = [base / lay for lay in layouts]
    smt_dir_cfg = cfg.get("submethods_dir")
    smt_path = (base / smt_dir_cfg).resolve() if smt_dir_cfg else None

    stale = Codegen.verify(
        layouts=layout_paths,
        submethods_dir=smt_path,
        output_dir=output_path,
    )
    if not stale:
        return stale

    summary = "; ".join(f"[{reason}] {src}" for src, reason in stale)
    msg = f"venusrpc: stale generated wrappers — run `venusrpc generate`: {summary}"
    if mode == "strict":
        raise StaleResourcesError(msg)
    warnings.warn(msg, stacklevel=2)
    return stale
