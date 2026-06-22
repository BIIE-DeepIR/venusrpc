"""Write a custom liquid class to a COPY of ML_VANTAGELiquids.mdb, regenerate
the Python wrapper, and verify the new class is usable via venusrpc.

The live Hamilton config DB is never mutated — every test copies the source
.mdb into a tmp dir first and writes only to that copy.

Run:
    uv run pytest tests/test_liquid_class_write.py -v -s

Requirements:
    - Microsoft Access ODBC driver
    - Hamilton Venus (ML_VANTAGELiquids.mdb present)
    - Venus runtime (for the live venusrpc round-trip test)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_SOURCE_DB = Path(r"C:\Program Files (x86)\HAMILTON\Config\ML_VANTAGELiquids.mdb")

pytestmark = pytest.mark.skipif(
    not _SOURCE_DB.exists(),
    reason="ML_VANTAGELiquids.mdb not found",
)

_TEST_LC_NAME = "VK_Test_Water_DispenseSurface"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A writable copy of the live liquid-class DB. Live config untouched."""
    copy = tmp_path / "ML_VANTAGELiquids.mdb"
    shutil.copy2(_SOURCE_DB, copy)
    return copy


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _connect(db: Path):
    import pyodbc

    drivers = [
        "Microsoft Access Driver (*.mdb, *.accdb)",
        "Microsoft Access Driver (*.mdb)",
    ]
    driver = next((d for d in drivers if d in pyodbc.drivers()), None)
    if driver is None:
        pytest.skip("No Microsoft Access ODBC driver installed")
    return pyodbc.connect(f"Driver={{{driver}}};DBQ={db};")


def _lc_exists(db: Path, name: str) -> bool:
    conn = _connect(db)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM LiquidClass WHERE LiquidClassName=?", name)
        return cursor.fetchone()[0] > 0
    finally:
        conn.close()


def _insert_lc(db: Path, name: str) -> None:
    """Insert a minimal custom water liquid class."""
    conn = _connect(db)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO LiquidClass (
                LiquidClassName, LiquidName, LiquidVersion, LiquidDevices,
                LiquidNotes, OriginalLiquid, DispenseMode, TipType,
                CorrectionCurve, PressureLLDSensitivity, LLDMaxHeightDifference,
                ValidationState, DesignHistory,
                AsFlowRate, AsMixFlowRate, AsAirTransportVolume, AsBlowOutVolume,
                AsSwapSpeed, AsSettlingTime, AsOverAspirateVolume, AsClotRetractHeight,
                DsFlowRate, DsMixFlowRate, DsAirTransportVolume, DsBlowOutVolume,
                DsSwapSpeed, DsSettlingTime, DsStopFlowRate, DsStopBackVolume,
                WhiPipJson, CheckSum
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?
            )
            """,
            name,                   # LiquidClassName
            "Water",                # LiquidName
            "1.0",                  # LiquidVersion
            "1",                    # LiquidDevices — 1ml channels
            b"\x00\x00",            # LiquidNotes
            0,                      # OriginalLiquid — 0 = custom
            5,                      # DispenseMode — surface dispense
            3,                      # TipType — standard 1ml filter tip
            b"\x00" * 30,           # CorrectionCurve — zeroed
            4,                      # PressureLLDSensitivity
            0.0,                    # LLDMaxHeightDifference
            1,                      # ValidationState
            b"\x01\x00\x00\x00\x00",  # DesignHistory
            # Aspirate params
            100.0,   # AsFlowRate
            100.0,   # AsMixFlowRate
            0.0,     # AsAirTransportVolume
            0.0,     # AsBlowOutVolume
            2.0,     # AsSwapSpeed
            0.0,     # AsSettlingTime
            0.0,     # AsOverAspirateVolume
            0.0,     # AsClotRetractHeight
            # Dispense params
            100.0,   # DsFlowRate
            100.0,   # DsMixFlowRate
            0.0,     # DsAirTransportVolume
            0.0,     # DsBlowOutVolume
            2.0,     # DsSwapSpeed
            0.0,     # DsSettlingTime
            50.0,    # DsStopFlowRate
            0.0,     # DsStopBackVolume
            None,    # WhiPipJson
            0,       # CheckSum — Hamilton computes this; 0 acceptable for custom
        )
        conn.commit()
    finally:
        conn.close()


def _delete_lc(db: Path, name: str) -> None:
    conn = _connect(db)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM LiquidClass WHERE LiquidClassName=?", name)
        conn.commit()
    finally:
        conn.close()


def _set_flow_rate(db: Path, name: str, as_flow_rate: float) -> None:
    """UPDATE the aspirate flow rate of an existing liquid class."""
    conn = _connect(db)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE LiquidClass SET AsFlowRate=? WHERE LiquidClassName=?",
            as_flow_rate,
            name,
        )
        conn.commit()
    finally:
        conn.close()


def _get_flow_rate(db: Path, name: str) -> float:
    conn = _connect(db)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT AsFlowRate FROM LiquidClass WHERE LiquidClassName=?", name
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_insert_and_read_back(db: Path):
    """INSERT a custom LC into the copy, verify it reads back correctly."""
    _insert_lc(db, _TEST_LC_NAME)
    assert _lc_exists(db, _TEST_LC_NAME), "LC not found after INSERT"

    conn = _connect(db)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT LiquidClassName, LiquidName, OriginalLiquid, DispenseMode "
            "FROM LiquidClass WHERE LiquidClassName=?",
            _TEST_LC_NAME,
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == _TEST_LC_NAME
    assert row[1] == "Water"
    assert row[2] == 0   # custom
    assert row[3] == 5   # surface dispense


def test_codegen_picks_up_new_class(db: Path, tmp_path: Path):
    """INSERT → codegen → new class appears in generated __liquid_classes__.py."""
    from venusrpc.codegen import Codegen

    _insert_lc(db, _TEST_LC_NAME)

    out_dir = tmp_path / "generated"
    out_dir.mkdir()
    Codegen.read_liquid_classes(
        database=db,
        include_1ml_channels=True,
        include_96head=False,
        include_custom=True,
        output_dir=out_dir,
    )

    generated = out_dir / "__liquid_classes__.py"
    assert generated.exists(), "codegen did not produce __liquid_classes__.py"

    content = generated.read_text(encoding="utf-8")
    assert _TEST_LC_NAME in content, (
        f"{_TEST_LC_NAME!r} missing from generated file"
    )


def test_modify_then_regenerate_flows_new_param(db: Path, tmp_path: Path):
    """INSERT → UPDATE AsFlowRate → codegen → new value appears in Python params."""
    from venusrpc.codegen import Codegen

    _insert_lc(db, _TEST_LC_NAME)
    assert _get_flow_rate(db, _TEST_LC_NAME) == 100.0

    # Modify the existing class.
    _set_flow_rate(db, _TEST_LC_NAME, 250.0)
    assert _get_flow_rate(db, _TEST_LC_NAME) == 250.0

    out_dir = tmp_path / "generated"
    out_dir.mkdir()
    Codegen.read_liquid_classes(
        database=db,
        include_1ml_channels=True,
        include_96head=False,
        include_custom=True,
        output_dir=out_dir,
    )

    content = (out_dir / "__liquid_classes__.py").read_text(encoding="utf-8")
    assert _TEST_LC_NAME in content
    # The regenerated params dict must carry the modified flow rate, not the old.
    assert "'AsFlowRate': 250.0" in content
    assert "'AsFlowRate': 100.0" not in content.split(_TEST_LC_NAME, 1)[1][:600]


def test_liquid_class_usable_as_venusrpc_type():
    """LiquidClass object built from the DB name renders correctly as HSL literal."""
    from venusrpc.types import LiquidClass

    lc = LiquidClass(name=_TEST_LC_NAME)
    assert str(lc) == _TEST_LC_NAME
    assert lc.__hsl_literal__() == f'"{_TEST_LC_NAME}"'


# ---------------------------------------------------------------------------
# Live venusrpc round-trip (skipped without Venus runtime)
# ---------------------------------------------------------------------------

_HXRUN = Path(r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe")
_HSL_REMOTE = Path(r"C:\Vantage\pyvenus\src\pyvenus\HSL\HSLremote.hsl")


@pytest.fixture(scope="module")
def con():
    """Live Venus process via the pyvenus file bridge (reused from test_integration)."""
    from tests.test_integration import _FileBridgeConnection

    with _FileBridgeConnection(start_minimized=True) as c:
        yield c


@pytest.mark.skipif(
    not _HXRUN.exists() or not _HSL_REMOTE.exists(),
    reason="Venus runtime or pyvenus HSL not found",
)
def test_new_lc_name_survives_venus_roundtrip(con):
    """New LC name passes through HSL as a string without Venus error."""
    from venusrpc.types import LiquidClass, Variable

    lc = LiquidClass(name=_TEST_LC_NAME)
    result_var = Variable(con, "", name="lc_name_result")
    # Assign the LC name string to a variable and read it back.
    con.execute(f'{result_var.name} = {lc.__hsl_literal__()};')
    result_var.pull()
    assert result_var.value == _TEST_LC_NAME
