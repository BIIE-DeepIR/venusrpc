"""Live-DB caching test: does Venus read the liquid-class DB at pipette time,
or cache it at process start?

This test MUTATES the live Hamilton config DB (ML_VANTAGELiquids.mdb): it
clones a known-good liquid class into a probe class, runs a simulated aspirate
referencing it, and deletes the probe afterwards. Because it touches live
config and launches a real Venus process, it is OPT-IN: it only runs when
VENUSRPC_LIVE_DB_TEST=1 is set.

    VENUSRPC_LIVE_DB_TEST=1 uv run --extra dev pytest \
        tests/test_liquid_class_live_db.py -v -s

Finding (2026-06-22, Venus 6.3.2): the probe class is "not found" before the
INSERT and resolves immediately after, within the SAME running Venus session —
Venus reads ML_VANTAGELiquids.mdb live per pipetting step; it does not cache
the liquid-class table at startup. An UPDATE to an existing class is likewise
picked up on the next step. A cloned class also needs its linked row in the
``Tadm`` table or the aspirate fails with "TADM data for liquid class not
found".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_LIVE = os.environ.get("VENUSRPC_LIVE_DB_TEST") == "1"
_DB = Path(r"C:\Program Files (x86)\HAMILTON\Config\ML_VANTAGELiquids.mdb")
_HXRUN = Path(r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe")

pytestmark = pytest.mark.skipif(
    not (_LIVE and _DB.exists() and _HXRUN.exists()),
    reason="opt-in live-DB test (set VENUSRPC_LIVE_DB_TEST=1; needs Venus + live DB)",
)

_SOURCE_LC = "Tip_50ulFilter_Water_DispenseSurface_Empty"
_PROBE_LC = "VK_Test_LiveDB_Probe"
_CH1 = "10000000"

_CLONE_COLS = [
    "LiquidName", "LiquidVersion", "LiquidDevices", "LiquidNotes",
    "OriginalLiquid", "DispenseMode", "TipType", "CorrectionCurve",
    "PressureLLDSensitivity", "LLDMaxHeightDifference", "ValidationState",
    "DesignHistory", "AsFlowRate", "AsMixFlowRate", "AsAirTransportVolume",
    "AsBlowOutVolume", "AsSwapSpeed", "AsSettlingTime", "AsOverAspirateVolume",
    "AsClotRetractHeight", "DsFlowRate", "DsMixFlowRate", "DsAirTransportVolume",
    "DsBlowOutVolume", "DsSwapSpeed", "DsSettlingTime", "DsStopFlowRate",
    "DsStopBackVolume", "WhiPipJson", "CheckSum",
]


def _connect():
    import pyodbc

    drv = next(
        d for d in (
            "Microsoft Access Driver (*.mdb, *.accdb)",
            "Microsoft Access Driver (*.mdb)",
        ) if d in pyodbc.drivers()
    )
    return pyodbc.connect(f"Driver={{{drv}}};DBQ={_DB};")


def _delete_probe() -> None:
    c = _connect()
    try:
        cur = c.cursor()
        cur.execute(
            "DELETE FROM Tadm WHERE LiquidClassID IN "
            "(SELECT LiquidClassID FROM LiquidClass WHERE LiquidClassName=?)",
            _PROBE_LC,
        )
        cur.execute("DELETE FROM LiquidClass WHERE LiquidClassName=?", _PROBE_LC)
        c.commit()
    finally:
        c.close()


def _insert_probe() -> int:
    c = _connect()
    try:
        cur = c.cursor()
        cur.execute(
            f"SELECT {', '.join(_CLONE_COLS)} FROM LiquidClass WHERE LiquidClassName=?",
            _SOURCE_LC,
        )
        vals = list(cur.fetchone())
        vals[_CLONE_COLS.index("OriginalLiquid")] = 0
        cols = ["LiquidClassName"] + _CLONE_COLS
        cur.execute(
            f"INSERT INTO LiquidClass ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            _PROBE_LC, *vals,
        )
        cur.execute("SELECT @@IDENTITY")
        new_id = cur.fetchone()[0]
        cur.execute(
            "SELECT TADMEnabled FROM Tadm WHERE LiquidClassID="
            "(SELECT LiquidClassID FROM LiquidClass WHERE LiquidClassName=?)",
            _SOURCE_LC,
        )
        row = cur.fetchone()
        cur.execute(
            "INSERT INTO Tadm (LiquidClassID, TADMEnabled, CheckSum) VALUES (?, ?, ?)",
            new_id, row[0] if row else 0, 0,
        )
        c.commit()
        return new_id
    finally:
        c.close()


def _aspirate(smt_ch, device, positions, lc: str) -> str:
    from venusrpc.connection import VenusError

    try:
        smt_ch.aspirate(
            ML_STAR=device,
            positions=positions,
            volume=10,
            liquid_class=lc,
            transfers_left=1,
            warning_volume=0,
            channel_pattern=_CH1,
            cLLD_sensitivity=1,
        )
        return "OK"
    except VenusError as e:
        return f"ERROR:{e.error_id}:{e.description}"


def test_venus_reads_liquid_class_db_live():
    """Insert a class mid-session and confirm Venus resolves it without restart."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pcr_resources.venus_resources.__layout__ngsstarv_deck import Ngsstarv_deck
    from pcr_resources.venus_resources.__smt__channels1ml_8 import Channels1ml_8
    from pcr_resources.venus_resources.__smt__ml_star import Ml_star
    from venusrpc import Connection, Device, Sequence

    _delete_probe()  # clean start
    try:
        with Connection(
            layout_file=Ngsstarv_deck.file,
            simulation=True,
            accept_timeout=120.0,
        ) as con:
            device = Device.from_existing(con)
            Ml_star(con).initialize(device)
            smt_ch = Channels1ml_8(con)
            tips = Sequence(
                con, Ngsstarv_deck.Sequences.All_50ul_tips, deck_sequence=True
            )
            tubes = Sequence(
                con,
                Ngsstarv_deck.Sequences.HCEppendorf_Adapter_24_tubes_T10_15_P4,
                deck_sequence=True,
            )
            smt_ch.tip_pickup(ML_STAR=device, positions=tips, channel_pattern=_CH1)

            before = _aspirate(smt_ch, device, tubes, _PROBE_LC)
            _insert_probe()  # mutate live DB while Venus runs
            after = _aspirate(smt_ch, device, tubes, _PROBE_LC)

        assert before.startswith("ERROR"), f"probe should be absent first: {before}"
        assert "Liquid class not found" in before
        assert after == "OK", f"Venus did not pick up the inserted LC live: {after}"
    finally:
        _delete_probe()  # restore live DB


def test_liquid_class_record_save_produces_venus_usable_class():
    """End-to-end via the public API: LiquidClassRecord.save() a new class
    mid-session, and confirm Venus aspirate resolves it."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pcr_resources.venus_resources.__layout__ngsstarv_deck import Ngsstarv_deck
    from pcr_resources.venus_resources.__smt__channels1ml_8 import Channels1ml_8
    from pcr_resources.venus_resources.__smt__ml_star import Ml_star
    from venusrpc import Connection, Device, LiquidClassRecord, Sequence

    _delete_probe()
    try:
        # Build the probe class from a known-good one, save() writes it + Tadm.
        src = LiquidClassRecord.load(_SOURCE_LC, _DB)
        probe = src.model_copy(update={
            "liquid_class_id": None,
            "liquid_class_name": _PROBE_LC,
            "original_liquid": 0,
            "as_flow_rate": 175.0,
        })

        with Connection(
            layout_file=Ngsstarv_deck.file,
            simulation=True,
            accept_timeout=120.0,
        ) as con:
            device = Device.from_existing(con)
            Ml_star(con).initialize(device)
            smt_ch = Channels1ml_8(con)
            tips = Sequence(
                con, Ngsstarv_deck.Sequences.All_50ul_tips, deck_sequence=True
            )
            tubes = Sequence(
                con,
                Ngsstarv_deck.Sequences.HCEppendorf_Adapter_24_tubes_T10_15_P4,
                deck_sequence=True,
            )
            smt_ch.tip_pickup(ML_STAR=device, positions=tips, channel_pattern=_CH1)

            before = _aspirate(smt_ch, device, tubes, _PROBE_LC)
            probe.save(database=_DB)  # public API write, Venus still running
            # The record is also directly usable as the aspirate argument.
            after = _aspirate(smt_ch, device, tubes, probe)

        assert before.startswith("ERROR")
        assert after == "OK", f"Venus did not resolve the saved record: {after}"
    finally:
        _delete_probe()
