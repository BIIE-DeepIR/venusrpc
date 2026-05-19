from __future__ import annotations

import pytest

from venusrpc._hsl import call, literal
from venusrpc.types import LiquidClass


def test_literal_renders_scalars() -> None:
    assert literal(True) == "1"
    assert literal(False) == "0"
    assert literal(42) == "42"
    assert literal(3.5) == "3.5"


def test_literal_escapes_strings() -> None:
    assert literal('a"b\\c\n') == r'"a\"b\\c\n"'


def test_literal_accepts_liquid_class() -> None:
    assert literal(LiquidClass('Tip "Water"')) == r'"Tip \"Water\""'


def test_literal_rejects_unsupported_values() -> None:
    with pytest.raises(TypeError):
        literal(object())


def test_call_renders_submethod_invocation() -> None:
    assert call("ML_STAR", "initialize", ["__DEVICE__", "1"]) == (
        "ML_STAR::initialize(__DEVICE__,1);"
    )
