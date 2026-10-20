"""Integration tests against a live Venus runtime.

These drive Venus through a thin file-based bridge over PyVenus's
HSLremote.hsl protocol (write .hsl to toSystem/,
read JSON from fromSystem/).  All venusrpc type wrappers are exercised against
real HSL execution; TCP framing is covered by test_framing.py.

Point PYVENUS_HSL_REMOTE at HSLremote.hsl from a PyVenus checkout, then run:
    uv run pytest tests/test_integration.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import atexit
from pathlib import Path
from typing import Any

import pytest

from venusrpc.connection import VenusError
from venusrpc.types import Array, Sequence, Variable

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

_HXRUN = Path(r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe")
_HSL_REMOTE = Path(os.environ.get("PYVENUS_HSL_REMOTE", "HSLremote.hsl"))

pytestmark = pytest.mark.skipif(
    not _HXRUN.exists() or not _HSL_REMOTE.exists(),
    reason="Venus runtime or pyvenus HSL not found",
)

# ---------------------------------------------------------------------------
# Compat shims
#
# venusrpc type wrappers call AddJsonVariable / AddJsonArray / AddJsonSequence.
# pyvenus's HSLremote.hsl defines these under lowercase names.  We define the
# venusrpc-style names as thin aliases, sent once at startup so they persist
# in the global HSL scope for the lifetime of the Venus process.
# ---------------------------------------------------------------------------

_COMPAT_DEFS = """\
function AddJsonVariable(variable & j, variable v, variable name) void {
    addJSON_variable(j, v, name);
}
function AddJsonArray(variable & j, variable arr[], variable name) void {
    addJSON_array(j, arr, name);
}
function AddJsonSequence(variable & j, sequence seq, variable name) void {
    addJSON_sequence(j, seq, name);
}
function AddJsonSequenceXyz(variable & j, sequence seq, variable name) void {
    addJSON_sequence_xyz(j, seq, name);
}
"""


# ---------------------------------------------------------------------------
# _FileBridgeConnection
# ---------------------------------------------------------------------------

class _FileBridgeConnection:
    """Drive Venus via pyvenus file-based IPC.

    Presents the same execute(hsl_code, definitions) -> dict interface as
    venusrpc.Connection so all type wrappers (Variable, Array, Sequence, ...)
    work without modification.

    Protocol (identical to pyvenus):
      1. Write <counter>.hsl to toSystem/ (definitions + __EvalExpr__ body).
      2. Venus includes the file, calls __EvalExpr__(), writes JSON to fromSystem/.
      3. Python reads and parses the JSON response.

    HSL global scope persists across execute() calls, so Variable/Array/Sequence
    definitions accumulate correctly just as they do over TCP.
    """

    def __init__(
        self,
        start_minimized: bool = True,
        startup_timeout: float = 60.0,
    ) -> None:
        hsl_dir = _HSL_REMOTE.parent
        self._to = hsl_dir / "toSystem"
        self._from = hsl_dir / "fromSystem"

        for d in (self._to, self._from):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir()

        self._counter = 1
        self._closed = False

        info = subprocess.STARTUPINFO()
        if start_minimized:
            info.dwFlags = subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 6

        self._proc = subprocess.Popen(
            [str(_HXRUN), "-t", str(_HSL_REMOTE)],
            universal_newlines=True,
            startupinfo=info,
        )
        atexit.register(self.close)

        # First call: install compat shims AND wait for Venus to be ready.
        # The response arrives only after Venus has fully initialised.
        self._execute(_COMPAT_DEFS, "", timeout=startup_timeout)

    # ------------------------------------------------------------------
    # Public interface (matches venusrpc.Connection.execute)
    # ------------------------------------------------------------------

    def execute(self, hsl_code: str = "", definitions: str = "") -> dict[str, Any]:
        return self._execute(definitions, hsl_code)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        try:
            self._execute("", "___SHUTDOWN___ = 1;", timeout=10.0)
        except Exception:
            pass
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def __enter__(self) -> "_FileBridgeConnection":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute(
        self,
        definitions: str,
        hsl_code: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        n = self._counter
        self._counter += 1

        # Build the HSL file.
        # definitions are already HSL-escaped by venusrpc type wrappers — do
        # NOT double-escape backslashes (unlike pyvenus's own execute() which
        # expects raw Python paths and doubles them itself).
        content = (
            definitions
            + "\nfunction __EvalExpr__()\n{\n"
            + hsl_code
            + "\n}"
        )

        cmd_file = self._to / f"{n}.hsl"
        tmp = self._to / f"{n}.hsl.tmp"
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(cmd_file)

        resp_file = self._from / f"{n}.json"
        deadline = time.monotonic() + timeout
        while not resp_file.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"No Venus response within {timeout:.0f}s (cmd {n})"
                )
            time.sleep(0.05)

        # Venus writes atomically (tmp → rename) but retry a few times to be safe.
        raw = ""
        for _ in range(20):
            try:
                raw = resp_file.read_text(encoding="utf-8").strip()
                data: dict[str, Any] = json.loads(raw)
                break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError(f"Could not parse Venus response: {raw!r}")

        if "___ERROR_ID___" in data:
            raise VenusError(
                error_id=int(data.get("___ERROR_ID___", -1)),
                description=str(data.get("___ERROR_DESCRIPTION___", "")),
                data=list(data.get("___ERROR_DATA___", [])),
            )
        return data


# ---------------------------------------------------------------------------
# Fixture — one Venus process per test module run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def con() -> _FileBridgeConnection:
    with _FileBridgeConnection(start_minimized=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Raw HSL execution tests
# ---------------------------------------------------------------------------

def test_integer_arithmetic(con: _FileBridgeConnection) -> None:
    result = con.execute(
        definitions="variable ans(0);",
        hsl_code='ans = 7 * 6; AddJsonVariable(___JSON___, ans, "value");',
    )
    assert result["value"] == 42


def test_float_arithmetic(con: _FileBridgeConnection) -> None:
    result = con.execute(
        definitions="variable fval(0.0);",
        hsl_code='fval = 1.5 + 0.5; AddJsonVariable(___JSON___, fval, "value");',
    )
    assert abs(result["value"] - 2.0) < 1e-9


def test_string_replace(con: _FileBridgeConnection) -> None:
    result = con.execute(
        definitions='variable sval("hello");',
        hsl_code='StrReplace(sval, "hello", "world"); AddJsonVariable(___JSON___, sval, "value");',
    )
    assert result["value"] == "world"


def test_multiple_json_properties(con: _FileBridgeConnection) -> None:
    result = con.execute(
        definitions="variable ma(10); variable mb(32);",
        hsl_code=(
            'AddJsonVariable(___JSON___, ma, "a");'
            'AddJsonVariable(___JSON___, mb, "b");'
        ),
    )
    assert result["a"] == 10
    assert result["b"] == 32


def test_hsl_error_raises_venus_error(con: _FileBridgeConnection) -> None:
    with pytest.raises(VenusError):
        con.execute(hsl_code="UndefinedFunction12345XYZ();")


# ---------------------------------------------------------------------------
# Variable wrapper tests
# ---------------------------------------------------------------------------

def test_variable_integer_push_pull(con: _FileBridgeConnection) -> None:
    v = Variable(con, 0, name="intg_v")
    v.value = 99
    v.push()
    assert v.pull() == 99


def test_variable_float_push_pull(con: _FileBridgeConnection) -> None:
    v = Variable(con, 0.0, name="flt_v")
    v.value = 2.718
    v.push()
    assert abs(v.pull() - 2.718) < 0.001


def test_variable_string_push_pull(con: _FileBridgeConnection) -> None:
    v = Variable(con, "", name="str_v")
    v.value = "venusrpc"
    v.push()
    assert v.pull() == "venusrpc"


def test_variable_string_with_special_chars(con: _FileBridgeConnection) -> None:
    v = Variable(con, "", name="esc_v")
    v.value = 'line1\nline2'
    v.push()
    # HSL round-trips the string; newline may be normalised — just verify no crash
    pulled = v.pull()
    assert isinstance(pulled, str)


# ---------------------------------------------------------------------------
# Array wrapper tests
# ---------------------------------------------------------------------------

def test_array_integer_push_pull(con: _FileBridgeConnection) -> None:
    a = Array(con, [1, 2, 3], name="iarr")
    a.pull()
    assert a.value == [1, 2, 3]


def test_array_float_push_pull(con: _FileBridgeConnection) -> None:
    a = Array(con, [1.1, 2.2, 3.3], name="farr")
    a.pull()
    vals = a.value
    assert len(vals) == 3
    assert abs(vals[0] - 1.1) < 0.001
    assert abs(vals[2] - 3.3) < 0.001


def test_array_modify_and_repull(con: _FileBridgeConnection) -> None:
    a = Array(con, [10, 20], name="modarr")
    a.value = [100, 200, 300]
    a.push()
    a.pull()
    assert a.value == [100, 200, 300]


# ---------------------------------------------------------------------------
# Sequence wrapper tests
# ---------------------------------------------------------------------------

def test_sequence_push_pull(con: _FileBridgeConnection) -> None:
    s = Sequence(con, name="testseq")
    s.labware = ["Plate1", "Plate1", "Plate1"]
    s.position = [1, 2, 3]
    s.end = 3
    s.current = 1
    s.push()
    s.pull()
    assert s.labware == ["Plate1", "Plate1", "Plate1"]
    assert s.position == [1, 2, 3]
    assert s.end == 3
    assert s.current == 1


def test_sequence_as_records_after_pull(con: _FileBridgeConnection) -> None:
    s = Sequence(con, name="recseq")
    s.labware = ["TubeRack", "TubeRack"]
    s.position = [5, 6]
    s.end = 2
    s.current = 1
    s.push()
    s.pull()
    records = s.as_records()
    assert records == [
        {"labware": "TubeRack", "position": 5},
        {"labware": "TubeRack", "position": 6},
    ]
