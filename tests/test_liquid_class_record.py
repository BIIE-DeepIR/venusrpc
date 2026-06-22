"""Tests for the typed, writable LiquidClassRecord model and its save() upsert.

Every test that touches a DB writes to a tmp copy of the live
ML_VANTAGELiquids.mdb (the ``db`` fixture); the live config is never mutated.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from venusrpc import DispenseMode, LiquidClassRecord

_SOURCE_DB = Path(r"C:\Program Files (x86)\HAMILTON\Config\ML_VANTAGELiquids.mdb")
_SOURCE_LC = "Tip_50ulFilter_Water_DispenseSurface_Empty"

pytestmark = pytest.mark.skipif(
    not _SOURCE_DB.exists(), reason="ML_VANTAGELiquids.mdb not found"
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    copy = tmp_path / "ML_VANTAGELiquids.mdb"
    shutil.copy2(_SOURCE_DB, copy)
    return copy


# ── model basics (no DB) ─────────────────────────────────────────────────────

def test_construct_from_db_column_aliases():
    rec = LiquidClassRecord(
        LiquidClassName="X",
        DispenseMode=5,
        TipType=23,
        AsFlowRate=100.0,
    )
    assert rec.liquid_class_name == "X"
    assert rec.as_flow_rate == 100.0
    assert rec.dispense_mode == 5


def test_construct_from_snake_case():
    rec = LiquidClassRecord(
        liquid_class_name="X", dispense_mode=5, tip_type=23, as_flow_rate=50.0
    )
    assert rec.as_flow_rate == 50.0


def test_usable_as_aspirate_argument():
    rec = LiquidClassRecord(liquid_class_name="My_LC", dispense_mode=5, tip_type=23)
    assert str(rec) == "My_LC"
    assert rec.__hsl_literal__() == '"My_LC"'


def test_by_alias_dump_uses_db_columns_and_excludes_bookkeeping():
    rec = LiquidClassRecord(
        liquid_class_name="X",
        dispense_mode=5,
        tip_type=23,
        as_flow_rate=10.0,
        source_database="ignored.mdb",
    )
    dumped = rec.model_dump(by_alias=True)
    assert "AsFlowRate" in dumped
    assert "source_database" not in dumped  # excluded from serialization


def test_dispense_mode_enum_is_int_compatible():
    rec = LiquidClassRecord(
        liquid_class_name="X", dispense_mode=DispenseMode.SURFACE, tip_type=23
    )
    assert rec.dispense_mode == 5


def test_save_without_database_raises():
    rec = LiquidClassRecord(liquid_class_name="X", dispense_mode=5, tip_type=23)
    with pytest.raises(ValueError, match="no database"):
        rec.save()


# ── save() round-trips against a DB copy ─────────────────────────────────────

def test_load_existing_class(db: Path):
    rec = LiquidClassRecord.load(_SOURCE_LC, db)
    assert rec is not None
    assert rec.liquid_class_name == _SOURCE_LC
    assert rec.source_database == str(db)
    assert rec.correction_curve is not None  # binary blob round-tripped


def test_load_missing_returns_none(db: Path):
    assert LiquidClassRecord.load("__does_not_exist__", db) is None


def test_save_updates_existing_class(db: Path):
    rec = LiquidClassRecord.load(_SOURCE_LC, db)
    original = rec.as_flow_rate
    rec.as_flow_rate = original + 123.0
    rec.save()  # uses source_database

    reloaded = LiquidClassRecord.load(_SOURCE_LC, db)
    assert reloaded.as_flow_rate == original + 123.0
    # Binary columns untouched by the partial update.
    assert reloaded.correction_curve == rec.correction_curve


def test_update_does_not_null_existing_blob(db: Path):
    """A None-valued field must not overwrite an existing column with NULL."""
    rec = LiquidClassRecord.load(_SOURCE_LC, db)
    assert rec.correction_curve is not None
    rec.correction_curve = None  # pretend caller cleared it locally
    rec.as_flow_rate = 99.0
    rec.save()

    reloaded = LiquidClassRecord.load(_SOURCE_LC, db)
    assert reloaded.as_flow_rate == 99.0
    assert reloaded.correction_curve is not None  # NOT nulled


def test_save_inserts_new_class_and_tadm_row(db: Path):
    import pyodbc

    name = "VK_Record_Insert_Probe"
    src = LiquidClassRecord.load(_SOURCE_LC, db)
    new = src.model_copy(update={
        "liquid_class_id": None,        # force INSERT
        "liquid_class_name": name,
        "original_liquid": 0,           # custom
        "as_flow_rate": 222.0,
    })
    new.save(database=db)
    assert new.liquid_class_id is not None  # populated from @@IDENTITY

    reloaded = LiquidClassRecord.load(name, db)
    assert reloaded is not None
    assert reloaded.as_flow_rate == 222.0

    # A linked Tadm row must exist (else Venus aspirate fails).
    drv = next(
        d for d in pyodbc.drivers() if d.startswith("Microsoft Access Driver")
    )
    conn = pyodbc.connect(f"Driver={{{drv}}};DBQ={db};")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM Tadm WHERE LiquidClassID=?", reloaded.liquid_class_id
        )
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_write_is_alias_for_save(db: Path):
    rec = LiquidClassRecord.load(_SOURCE_LC, db)
    rec.as_flow_rate = 77.0
    rec.write()
    assert LiquidClassRecord.load(_SOURCE_LC, db).as_flow_rate == 77.0


def test_delete_removes_class_and_tadm(db: Path):
    name = "VK_Record_Delete_Probe"
    src = LiquidClassRecord.load(_SOURCE_LC, db)
    src.model_copy(update={
        "liquid_class_id": None, "liquid_class_name": name, "original_liquid": 0
    }).save(database=db)
    assert LiquidClassRecord.load(name, db) is not None

    LiquidClassRecord(
        liquid_class_name=name, dispense_mode=5, tip_type=23, source_database=str(db)
    ).delete()
    assert LiquidClassRecord.load(name, db) is None
