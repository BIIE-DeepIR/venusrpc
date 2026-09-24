"""Type wrapper tests against a mock Connection that records every execute()."""

from __future__ import annotations

from typing import Any

import pytest

from venusrpc.helpers import Helpers
from venusrpc.types import Array, Device, LiquidClass, Sequence, Variable


class MockConnection:
    def __init__(self, return_queue: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.return_queue = list(return_queue or [])

    def execute(self, hsl_code: str = "", definitions: str = "") -> dict[str, Any]:
        self.calls.append({"hsl_code": hsl_code, "definitions": definitions})
        if self.return_queue:
            return self.return_queue.pop(0)
        return {}


def test_variable_definition_and_push():
    con = MockConnection()
    v = Variable(con, 7, name="x")
    assert con.calls[0]["definitions"] == "variable x(7);"

    v.value = 99
    v.push()
    assert con.calls[1]["hsl_code"] == "x = 99;"


def test_variable_string_literal_is_escaped():
    con = MockConnection()
    v = Variable(con, 'hello "world"', name="s")
    assert con.calls[0]["definitions"] == r'variable s("hello \"world\"");'


def test_variable_pull_updates_value():
    con = MockConnection(return_queue=[{}, {"value": 123}])
    v = Variable(con, 0, name="v")
    assert v.pull() == 123
    assert v.value == 123
    assert con.calls[-1]["hsl_code"] == 'AddJsonVariable(___JSON___, v, "value");'


def test_array_push_and_pull():
    con = MockConnection(return_queue=[{}, {}, {"value": [1, 2, 3]}])
    a = Array(con, [1, 2, 3], name="arr")
    # __init__ declared then pushed
    assert con.calls[0]["definitions"] == "variable arr[];"
    push_hsl = con.calls[1]["hsl_code"]
    assert push_hsl.startswith("arr.SetSize(0);")
    assert "arr.AddAsLast(1);" in push_hsl
    assert "arr.AddAsLast(3);" in push_hsl

    a.pull()
    assert a.value == [1, 2, 3]


def test_sequence_push_serializes_records():
    con = MockConnection()
    s = Sequence(con, name="seq")
    s.labware = ["lab1", "lab2"]
    s.position = [1, 5]
    s.end = 2
    s.current = 1
    s.push()
    hsl = con.calls[-1]["hsl_code"]
    assert 'seq.Add("lab1", "1");' in hsl
    assert 'seq.Add("lab2", "5");' in hsl
    assert "seq.SetCount(2);" in hsl
    assert "seq.SetCurrentPosition(1);" in hsl


def test_sequence_pull_parses_response():
    payload = {
        "value": {
            "labware": ["a", "b"],
            "position": [1, 2],
            "current": 2,
            "end": 2,
        }
    }
    con = MockConnection(return_queue=[payload])
    s = Sequence(con, name="seq", deck_sequence=True)
    # deck_sequence=True skips the definition call, so queue head is for pull()
    s.pull()
    assert s.labware == ["a", "b"]
    assert s.position == [1, 2]
    assert s.current == 2
    assert s.end == 2


def test_liquid_class_renders_as_string_literal():
    lc = LiquidClass("HighVolume_Water_DispenseJet_Empty")
    con = MockConnection()
    v = Variable(con, lc.name, name="liq")
    # Initialise as a string literal
    assert "HighVolume_Water_DispenseJet_Empty" in con.calls[0]["definitions"]


def test_helpers_well_addressing():
    assert Helpers.numeric_to_alphanumeric(1) == "A1"
    assert Helpers.numeric_to_alphanumeric(8) == "H1"
    assert Helpers.numeric_to_alphanumeric(9) == "A2"
    assert Helpers.numeric_to_alphanumeric(96) == "H12"
    assert Helpers.numeric_to_alphanumeric(96, zero_padding=True) == "H12"
    assert Helpers.numeric_to_alphanumeric(1, zero_padding=True) == "A01"
    assert Helpers.numeric_to_alphanumeric(1, sorting_mode="row") == "A1"
    assert Helpers.numeric_to_alphanumeric(2, sorting_mode="row") == "A2"
    assert Helpers.alphanumeric_to_numeric("H1") == 8
    assert Helpers.alphanumeric_to_numeric("A2") == 9
    assert Helpers.alphanumeric_to_numeric("H12") == 96


def test_helpers_invalid_well():
    with pytest.raises(ValueError):
        Helpers.numeric_to_alphanumeric(0)
    with pytest.raises(ValueError):
        Helpers.numeric_to_alphanumeric(97)
    with pytest.raises(ValueError):
        Helpers.alphanumeric_to_numeric("Z99")


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

def test_device_creates_definition_and_sets_main():
    con = MockConnection()
    d = Device(con, r"C:\path\to\deck.lay")
    assert "device" in con.calls[0]["definitions"]
    assert "deck.lay" in con.calls[0]["definitions"]
    assert con.calls[1]["hsl_code"] == f"__DEVICE__ = {d.name};"


def test_device_no_main():
    con = MockConnection()
    d = Device(con, r"C:\path\to\deck.lay", main=False)
    assert len(con.calls) == 1
    assert "device" in con.calls[0]["definitions"]


# ---------------------------------------------------------------------------
# Sequence extras
# ---------------------------------------------------------------------------

def test_sequence_as_records():
    con = MockConnection()
    s = Sequence(con, name="seq")
    s.labware = ["lab1", "lab2"]
    s.position = [1, 5]
    assert s.as_records() == [
        {"labware": "lab1", "position": 1},
        {"labware": "lab2", "position": 5},
    ]


def test_sequence_copy_from():
    payload = {"value": {"labware": ["plate"], "position": [3], "current": 1, "end": 1}}
    # Sequence.__init__ makes 3 execute() calls when copy_from is set and
    # deck_sequence is False: (0) declare, (1) assign, (2) pull.
    # Supply {} for the first two so the payload reaches pull().
    con = MockConnection(return_queue=[{}, {}, payload])
    s = Sequence(con, name="dest", copy_from="source_seq")
    assert con.calls[1]["hsl_code"] == "dest = source_seq;"
    assert s.labware == ["plate"]
    assert s.current == 1


def test_sequence_pull_with_xyz():
    payload = {
        "value": {
            "labware": ["a", "b"],
            "position": [1, 2],
            "current": 1,
            "end": 2,
            "x": [10.0, 20.0],
            "y": [5.0, 15.0],
            "z": [1.0, 1.5],
        }
    }
    # Sequence.__init__ makes 1 execute() call for deck_sequence=True: pull_full().
    # Supply {} for that so the payload reaches pull_with_xyz().
    con = MockConnection(return_queue=[{}, payload])
    s = Sequence(con, name="seq", deck_sequence=True)
    data = s.pull_with_xyz()
    assert s.labware == ["a", "b"]
    assert s.position == [1, 2]
    assert s.current == 1
    assert data["x"] == [10.0, 20.0]
    assert data["z"] == [1.0, 1.5]


# ---------------------------------------------------------------------------
# Array extras
# ---------------------------------------------------------------------------

def test_array_empty_init():
    con = MockConnection()
    a = Array(con, name="arr")
    assert len(con.calls) == 1  # only declaration, no push
    assert a.value == []


def test_array_value_setter_and_push():
    con = MockConnection()
    a = Array(con, [1, 2], name="arr")
    a.value = [10, 20, 30]
    a.push()
    last = con.calls[-1]["hsl_code"]
    assert "arr.SetSize(0);" in last
    assert "arr.AddAsLast(10);" in last
    assert "arr.AddAsLast(30);" in last


# ---------------------------------------------------------------------------
# Variable extras
# ---------------------------------------------------------------------------

def test_variable_bool_value():
    con = MockConnection()
    v = Variable(con, True, name="flag")
    assert "flag(1)" in con.calls[0]["definitions"]
    v.value = False
    v.push()
    assert con.calls[1]["hsl_code"] == "flag = 0;"


def test_variable_float_value():
    con = MockConnection()
    v = Variable(con, 3.14, name="pi")
    assert "pi(3.14)" in con.calls[0]["definitions"]
    v.push()
    assert con.calls[1]["hsl_code"] == "pi = 3.14;"


def test_variable_autoname_unique():
    con = MockConnection()
    v1 = Variable(con, 0)
    v2 = Variable(con, 0)
    assert v1.name != v2.name


# ---------------------------------------------------------------------------
# Helpers extras
# ---------------------------------------------------------------------------

def test_helpers_384_well_plate():
    assert Helpers.numeric_to_alphanumeric(1, plate_type=384) == "A1"
    assert Helpers.numeric_to_alphanumeric(16, plate_type=384) == "P1"
    assert Helpers.numeric_to_alphanumeric(17, plate_type=384) == "A2"
    assert Helpers.alphanumeric_to_numeric("A1", plate_type=384) == 1
    assert Helpers.alphanumeric_to_numeric("P1", plate_type=384) == 16


def test_helpers_get_well_map_length():
    assert len(Helpers.get_well_map(96)) == 96
    assert len(Helpers.get_well_map(384)) == 384
    assert Helpers.get_well_map(96)[0] == "A1"
    assert Helpers.get_well_map(96)[-1] == "H12"
