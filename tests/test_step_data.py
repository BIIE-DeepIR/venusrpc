from __future__ import annotations

import pytest

from venusrpc import step_data

# Shaped like a real Hamilton .stp: two step blocks, nested error/recovery
# structures that close with ")" and ");", and the block itself with "];".
STP = "\r\n".join([
    "HxCfgFile,3;",
    "",
    "ConfigIsValid,Y;",
    "",
    "DataDef,HxPars,3,aaaa_1111,",
    "[",
    '"1CommandStepFileGuid",',
    '"aaaa_1111",',
    '"3LockFrontCoverState",',
    '"0",',
    '"(Errors",',
    '"(3",',
    '"3RepeatCount",',
    '"0",',
    '")"',
    "];",
    "",
    "DataDef,HxPars,3,bbbb_2222,",
    "[",
    '"1CommandStepFileGuid",',
    '"bbbb_2222",',
    '"3LockFrontCoverState",',
    '"1",',
    "];",
    "",
])


@pytest.fixture
def stp(tmp_path):
    path = tmp_path / "lib.stp"
    path.write_bytes(STP.encode("latin-1"))
    return path


def test_read_returns_one_block_as_a_whole_stp(stp) -> None:
    doc = step_data.read("aaaa_1111", stp)
    assert doc.startswith("HxCfgFile,3;\r\n\r\nConfigIsValid,Y;\r\n\r\nDataDef")
    assert doc.endswith("];\r\n")
    assert '"3LockFrontCoverState",\r\n"0",' in doc
    # The neighbouring block must not be dragged in: the nested ")" lines are
    # not block terminators, but the "];" that follows them is.
    assert "bbbb_2222" not in doc


def test_read_finds_a_later_block(stp) -> None:
    doc = step_data.read("bbbb_2222", stp)
    assert '"3LockFrontCoverState",\r\n"1",' in doc
    assert "aaaa_1111" not in doc


def test_read_rejects_an_unknown_guid(stp) -> None:
    with pytest.raises(step_data.StepDataError):
        step_data.read("cccc_3333", stp)


def test_read_resolves_a_bare_name_against_the_library() -> None:
    stp = step_data.LIBRARY_DIR / "HslStarLineMaintMetLib.stp"
    if not stp.exists():
        pytest.skip("no Venus installation")
    # The two cover steps venusrpc calls by guid, in Hamilton's own library.
    for guid in ("13358f08_1f67_4dcd_ac2dd6c120e15c21",
                 "ca965d5b_e567_47e5_a8d23f2768eee8ec"):
        assert guid in step_data.read(guid, "HslStarLineMaintMetLib.stp")
