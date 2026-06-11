"""Unit tests for labware-definition parsing helpers (no Venus required)."""

from venusrpc.codegen import (
    Codegen,
    _flat_float,
    _flat_int,
    _parse_flat_hxcfg,
    _safe_volume,
)

_SAMPLE_RCK = """HxCfgFile,2;

ConfigIsValid,Y;

DataDef,RECTRACK,3,default,
{
Dim.Dx, "127",
Dim.Dy, "86",
Dim.Dz, "14.5",
Columns, "12",
Rows, "8",
Dx, "9",
Dy, "9",
StackHt, "13",
Cntr.1.file, "corning-costar\\\\cos_96_halfarea.ctr",
SegmentCount_x, "2",
Seg_x.0.LowerWidth, "125.6",
Seg_x.0.UpperWidth, "125.6",
Seg_x.0.SegmentHeight, "8",
Seg_x.1.LowerWidth, "127.7",
Seg_x.1.UpperWidth, "127.7",
Seg_x.1.SegmentHeight, "6",
Description, "",
Visible, "0"
};
"""


def test_parse_flat_hxcfg_reads_keys_and_values():
    d = _parse_flat_hxcfg(_SAMPLE_RCK)
    assert d["Dim.Dx"] == "127"
    assert d["Seg_x.1.UpperWidth"] == "127.7"
    assert d["Description"] == ""  # empty value preserved
    # structural lines (DataDef/braces) are not key-value pairs
    assert "DataDef" not in d


def test_flat_float_and_int_coercion():
    d = _parse_flat_hxcfg(_SAMPLE_RCK)
    assert _flat_float(d, "Dim.Dz") == 14.5
    assert _flat_int(d, "Rows") == 8
    assert _flat_float(d, "Missing") is None
    assert _flat_int(d, "Missing") is None


def test_segments_extracted_bottom_up():
    d = _parse_flat_hxcfg(_SAMPLE_RCK)
    segs = Codegen._segments(d, "x")
    assert segs == ((125.6, 125.6, 8.0), (127.7, 127.7, 6.0))
    # No SegmentCount_y in the sample -> empty profile.
    assert Codegen._segments(d, "y") == ()


def test_safe_volume_evaluates_simple_equation():
    assert _safe_volume("h*17.7369", 10.7) == round(10.7 * 17.7369, 4)
    # power operator is translated from Hamilton '^'
    assert _safe_volume("h^2", 3.0) == 9.0


def test_safe_volume_rejects_unsafe_or_missing():
    assert _safe_volume("__import__('os')", 1.0) is None
    assert _safe_volume("h*2", None) is None
    assert _safe_volume(None, 1.0) is None
    assert _safe_volume("abs(h)", 1.0) is None  # names other than h are rejected
