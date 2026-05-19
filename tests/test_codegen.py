"""Codegen smoke tests for the Jinja templates."""

from __future__ import annotations

import pytest
from jinja2 import Environment, PackageLoader, select_autoescape

from venusrpc.codegen import _Function, _Parameter, _Submethod, _sanitize_identifier


@pytest.fixture
def env() -> Environment:
    return Environment(
        loader=PackageLoader("venusrpc", "templates"),
        autoescape=select_autoescape(["html", "xml"]),
        keep_trailing_newline=True,
    )


def test_deck_layout_template_renders(env: Environment):
    out = env.get_template("deck_layout.py.j2").render(
        layout_file="C:\\\\path\\\\to\\\\deck.lay",
        layout_name="my_deck",
        sequences=["WashStation", "Plate1"],
        labware=["TipRack_1000ul", "Plate96"],
    )
    assert "class My_deck:" in out
    assert "WashStation" in out
    assert "TipRack_1000ul" in out
    assert "class Sequences" in out
    assert "class Labware" in out


def test_submethod_template_renders(env: Environment):
    sm = _Submethod(
        name="pipetting",
        file="C:\\\\path\\\\Pipetting.hs_",
        functions=[
            _Function(
                name="aspirate",
                has_return=False,
                parameters=[
                    _Parameter(
                        name="device",
                        hsl_type="device",
                        py_type="Device",
                        is_array=False,
                        direction="in",
                        pass_mode="variable",
                        comment="STAR device",
                    ),
                    _Parameter(
                        name="volume",
                        hsl_type="variable",
                        py_type="Variable",
                        is_array=False,
                        direction="in",
                        pass_mode="value",
                        comment="aspirate volume in uL",
                        default_value="50",
                    ),
                ],
                comment="aspirate liquid",
            ),
        ],
    )
    out = env.get_template("submethods.py.j2").render(submethod=sm)
    assert "class Pipetting:" in out
    assert "def aspirate(self" in out
    assert "volume=50" in out
    assert '_hsl_call("PIPETTING", "aspirate", parts)' in out
    assert "self._con.execute" in out


def test_liquid_classes_template_renders(env: Environment):
    import pprint

    out = env.get_template("liquid_classes.py.j2").render(
        liquid_classes=[
            {
                "attr": "Water_DispenseSurface",
                "name": "Water_DispenseSurface",
                "parameters_repr": pprint.pformat({"a": 1, "b": "x"}),
            },
            {
                "attr": "Plasma_Empty_Tip",
                "name": "Plasma-Empty.Tip",
                "parameters_repr": pprint.pformat({}),
            },
            {
                "attr": "_300ul_Tip",
                "name": "300ul Tip",
                "parameters_repr": pprint.pformat({"v": None}),
            },
        ]
    )
    assert "class LiquidClasses" in out
    assert "Water_DispenseSurface" in out
    assert "Plasma_Empty_Tip" in out
    assert "_300ul_Tip" in out
    assert "None" in out
    assert "null" not in out


# ---------------------------------------------------------------------------
# _sanitize_identifier
# ---------------------------------------------------------------------------

def test_sanitize_identifier_plain():
    assert _sanitize_identifier("my_layout") == "my_layout"
    assert _sanitize_identifier("_ok") == "_ok"


def test_sanitize_identifier_strips_leading_digits():
    # Digits before the first alpha/underscore are dropped.
    assert _sanitize_identifier("123abc") == "abc"


def test_sanitize_identifier_strips_disallowed_chars():
    assert _sanitize_identifier("Plasma-Empty.Tip") == "PlasmaEmptyTip"
    assert _sanitize_identifier("hello world") == "helloworld"


# ---------------------------------------------------------------------------
# Submethod template — array and out/in_out parameter handling
# ---------------------------------------------------------------------------

def test_submethod_template_with_array_param(env: Environment):
    """Array (variable[]) params are typed as Array and pushed before the call."""
    sm = _Submethod(
        name="pipetting",
        file="C:\\\\path\\\\Pipetting.hs_",
        functions=[
            _Function(
                name="aspirate",
                has_return=False,
                parameters=[
                    _Parameter(
                        name="ML_STAR",
                        hsl_type="device",
                        py_type="Device",
                        is_array=False,
                        direction="in",
                        pass_mode="variable",
                        comment="STAR device",
                    ),
                    _Parameter(
                        name="volumes",
                        hsl_type="variable",
                        py_type="Array",
                        is_array=True,
                        direction="in",
                        pass_mode="variable",
                        comment="per-channel volumes",
                    ),
                ],
                comment="aspirate with per-channel volumes",
            ),
        ],
    )
    out = env.get_template("submethods.py.j2").render(submethod=sm)
    assert "volumes: Array" in out
    assert "volumes.push()" in out
    # Device params must NOT be pushed
    assert "ML_STAR.push()" not in out


def test_submethod_template_out_param_pushes_and_pulls(env: Environment):
    """out-direction params are both pushed (to initialise HSL side) and pulled after."""
    sm = _Submethod(
        name="reader",
        file="C:\\\\path\\\\Reader.hs_",
        functions=[
            _Function(
                name="get_value",
                has_return=False,
                parameters=[
                    _Parameter(
                        name="result",
                        hsl_type="variable",
                        py_type="Variable",
                        is_array=False,
                        direction="out",
                        pass_mode="variable",
                        comment="output value",
                    ),
                ],
                comment="read a value",
            ),
        ],
    )
    out = env.get_template("submethods.py.j2").render(submethod=sm)
    assert "result.push()" in out
    assert "result.pull()" in out


def test_submethod_template_inout_param(env: Environment):
    """in_out sequence params are pushed before and pulled after the call."""
    sm = _Submethod(
        name="updater",
        file="C:\\\\path\\\\Updater.hs_",
        functions=[
            _Function(
                name="update",
                has_return=False,
                parameters=[
                    _Parameter(
                        name="seq",
                        hsl_type="sequence",
                        py_type="Sequence",
                        is_array=False,
                        direction="in_out",
                        pass_mode="variable",
                        comment="sequence to update",
                    ),
                ],
                comment="update a sequence",
            ),
        ],
    )
    out = env.get_template("submethods.py.j2").render(submethod=sm)
    assert "seq.push()" in out
    assert "seq.pull()" in out
    assert '_hsl_call("UPDATER", "update", parts)' in out


def test_submethod_template_in_variable_is_passed_by_value(env: Environment):
    """in Variable params use pass_mode='value': passed as a literal, not pushed."""
    sm = _Submethod(
        name="setter",
        file="C:\\\\path\\\\Setter.hs_",
        functions=[
            _Function(
                name="set_volume",
                has_return=False,
                parameters=[
                    _Parameter(
                        name="volume",
                        hsl_type="variable",
                        py_type="Variable",
                        is_array=False,
                        direction="in",
                        pass_mode="value",
                        comment="volume in uL",
                        default_value="100",
                    ),
                ],
                comment="set a volume",
            ),
        ],
    )
    out = env.get_template("submethods.py.j2").render(submethod=sm)
    # Default value present in signature
    assert "volume=100" in out
    # Value params are NOT pushed and NOT pulled
    assert "volume.push()" not in out
    assert "volume.pull()" not in out
