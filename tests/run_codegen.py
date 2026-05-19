"""Regenerate venus_resources for the PCR run test.

Run before test_pcr_run.py whenever lay/smt sources change:
    uv run python tests/run_codegen.py
"""

from pathlib import Path

from venusrpc.codegen import Codegen

_PCR_RESOURCES = Path(__file__).resolve().parent / "pcr_resources"
_VENUS_RESOURCES = _PCR_RESOURCES / "venus_resources"

if __name__ == "__main__":
    Codegen.read_layout(
        layout=_PCR_RESOURCES / "lay" / "NGSSTARV_Deck.lay",
        output_dir=_VENUS_RESOURCES,
    )
    Codegen.read_submethods(
        directory=_PCR_RESOURCES / "smt",
        output_dir=_VENUS_RESOURCES,
    )
