"""
PCR run smoke test — no UI, no deployment imports.

Uses resources copied into tests/pcr_resources/:
  lay/NGSSTARV_Deck.lay   — deck layout
  smt/channels1mL_8.*     — 8-channel pipetting
  smt/ml_star.*           — instrument init

Sequence:
  1. Initialize STAR
  2. Pick up one 50 µL tip (ch1 only)
  3. Aspirate 10 µL from water tube A6 on tube rack
  4. Dispense 10 µL to target plate A1
  5. Eject tip to default waste

Requires Venus running with venusrpc_host.hsl loaded.

Run:
    uv run python tests/test_pcr_run.py
"""

import sys
import time
from pathlib import Path

_PCR_RESOURCES = Path(__file__).resolve().parent / "pcr_resources"

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `pcr_resources` is importable as package

from venusrpc import Connection, Device, Sequence

from pcr_resources.venus_resources.__layout__ngsstarv_deck import Ngsstarv_deck
from pcr_resources.venus_resources.__smt__ml_star import Ml_star
from pcr_resources.venus_resources.__smt__channels1ml_8 import Channels1ml_8

# ── Liquid class used for water transfers ────────────────────────────────────
LC_WATER = "Tip_50ulFilter_Water_DispenseSurface_Empty"

# ── Channel pattern: only channel 1 active ───────────────────────────────────
CH1 = '10000000'


def main() -> None:
    print("Connecting to Venus...")
    with Connection(layout_file=Ngsstarv_deck.file) as con:
        device = Device.from_existing(con)
        smt_star = Ml_star(con)
        smt_ch = Channels1ml_8(con)

        tips_50 = Sequence(con, Ngsstarv_deck.Sequences.All_50ul_tips, deck_sequence=True)
        tube_rack = Sequence(con, Ngsstarv_deck.Sequences.HCEppendorf_Adapter_24_tubes_T10_15_P4, deck_sequence=True)
        target_plate = Sequence(con, Ngsstarv_deck.Sequences.azenta_framestar_96_PCR_T58_63_P3, deck_sequence=True)

        # Step 1 — initialize
        print("Initializing STAR...")
        smt_star.initialize(device)

        # Step 2 — pick up one tip
        print("Picking up tip (ch1)...")
        smt_ch.tip_pickup(
            ML_STAR=device,
            positions=tips_50,
            channel_pattern=CH1,
        )

        # Step 3 — aspirate 10 µL from water tube A6
        print("Aspirating 10 µL from water tube...")
        smt_ch.aspirate(
            ML_STAR=device,
            positions=tube_rack,
            volume=10,
            liquid_class=LC_WATER,
            transfers_left=1,
            warning_volume=0,
            channel_pattern=CH1,
            cLLD_sensitivity=1,
        )

        # Step 4 — dispense to target plate A1
        print("Dispensing to target plate A1...")
        smt_ch.dispense(
            ML_STAR=device,
            positions=target_plate,
            volume=10,
            channel_pattern=CH1,
            cLLD_sensitivity=0,
            fixed_height=2,
        )

        # Step 5 — eject tip
        print("Ejecting tip...")
        smt_ch.tip_eject(
            ML_STAR=device,
            positions=tips_50,
            default_waste=1,
            channel_pattern=CH1,
        )

        print("\nDone.")


_VENUS_LOG_DIR = Path(r"C:\Program Files (x86)\HAMILTON\LogFiles")


def _dump_latest_trace() -> None:
    trc_files = list(_VENUS_LOG_DIR.glob("venusrpc_host_*_Trace.trc"))
    if not trc_files:
        print("\n[trace] no venusrpc trace files found")
        return
    latest = max(trc_files, key=lambda f: f.stat().st_mtime)
    print(f"\n{'='*60}")
    print(f"[trace] {latest.name}")
    print('='*60)
    print(latest.read_text(encoding="utf-8", errors="replace").encode(
        sys.stdout.encoding or "utf-8", errors="replace"
    ).decode(sys.stdout.encoding or "utf-8", errors="replace"))


if __name__ == "__main__":
    t0 = time.monotonic()
    try:
        main()
    finally:
        _dump_latest_trace()
        print(f"\nelapsed: {time.monotonic() - t0:.1f}s")
