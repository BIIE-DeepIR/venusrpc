"""Toggle Hamilton Venus simulation mode.

Venus stores simulation flags in two binary cfg files under
``C:\\Program Files (x86)\\HAMILTON\\Config\\``:

* ``HxServices.cfg`` — key ``SimulationOn`` (global services flag)
* ``ML_STAR.cfg``    — key ``SimulateMode`` (ML_STAR instrument flag)

The on-disk format is ``<key>\\x01<'0'|'1'>``. The value is a single
ASCII byte, so toggling never shifts file length and the surrounding
binary structure stays intact. Writing the file requires admin
privileges (Program Files ACL).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = Path(r"C:\Program Files (x86)\HAMILTON\Config")

# (filename, key) pairs that together represent Venus simulation state.
_TARGETS: tuple[tuple[str, bytes], ...] = (
    ("HxServices.cfg", b"SimulationOn"),
    ("ML_STAR.cfg", b"SimulateMode"),
)

_SEP = b"\x01"
_BACKUP_SUFFIX = ".venusrpc.bak"


@dataclass(frozen=True)
class SimulationState:
    """Per-file simulation flag state."""

    path: Path
    key: str
    enabled: bool | None  # None => key not found in file

    @property
    def label(self) -> str:
        return f"{self.path.name}:{self.key}"


def _find_flag(data: bytes, key: bytes) -> int:
    """Return index of the value byte after ``key\\x01``, or -1."""
    needle = key + _SEP
    idx = data.find(needle)
    if idx < 0:
        return -1
    return idx + len(needle)


def _read_state(path: Path, key: bytes) -> bool | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    pos = _find_flag(data, key)
    if pos < 0 or pos >= len(data):
        return None
    byte = data[pos:pos + 1]
    if byte == b"1":
        return True
    if byte == b"0":
        return False
    return None


def _write_flag(path: Path, key: bytes, on: bool) -> bool:
    """Patch single value byte. Returns True if file changed."""
    data = path.read_bytes()
    pos = _find_flag(data, key)
    if pos < 0 or pos >= len(data):
        raise RuntimeError(f"key {key!r} not found in {path}")
    new_byte = b"1" if on else b"0"
    if data[pos:pos + 1] == new_byte:
        return False
    backup = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_bytes(data)
    patched = data[:pos] + new_byte + data[pos + 1:]
    path.write_bytes(patched)
    return True


def get_simulation(config_dir: Path | None = None) -> list[SimulationState]:
    """Return current simulation state for each known cfg target."""
    root = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    return [
        SimulationState(path=root / fname, key=key.decode("ascii"),
                        enabled=_read_state(root / fname, key))
        for fname, key in _TARGETS
    ]


def set_simulation(on: bool, config_dir: Path | None = None) -> list[SimulationState]:
    """Set Venus simulation flag in every known cfg target.

    Requires write access to ``config_dir`` (Program Files needs admin).
    Creates a ``*.cfg.venusrpc.bak`` on first toggle of each file.
    """
    root = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    results: list[SimulationState] = []
    for fname, key in _TARGETS:
        path = root / fname
        if not path.exists():
            results.append(SimulationState(path=path, key=key.decode("ascii"),
                                           enabled=None))
            continue
        _write_flag(path, key, on)
        results.append(SimulationState(path=path, key=key.decode("ascii"),
                                       enabled=on))
    return results


def _fmt(state: SimulationState) -> str:
    if state.enabled is None:
        return f"{state.label} = <missing> ({state.path})"
    val = "ON" if state.enabled else "OFF"
    return f"{state.label} = {val}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m venusrpc.simulation",
        description="Toggle Hamilton Venus simulation flags.",
    )
    parser.add_argument("action", choices=("status", "on", "off"),
                        help="status: read flags; on/off: write flags")
    parser.add_argument("--config-dir", default=None,
                        help=f"Venus config directory (default: {DEFAULT_CONFIG_DIR})")
    args = parser.parse_args(argv)

    cfg = Path(args.config_dir) if args.config_dir else None
    try:
        states = (get_simulation(cfg) if args.action == "status"
                  else set_simulation(args.action == "on", cfg))
    except PermissionError as exc:
        print(f"permission denied (need admin?): {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for s in states:
        print(_fmt(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
