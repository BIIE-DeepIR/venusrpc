"""Provoke a Venus error: pick up a tip from the same position twice.

First pickup empties the tip slot. Second pickup at the same slot should fail
because no tip is present. Demonstrates VenusError surfacing from the runtime.

Run:
    uv run python tests/test_pcr_double_pickup.py
"""

import sys
import time
from pathlib import Path

_PCR_RESOURCES = Path(__file__).resolve().parent / "pcr_resources"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from venusrpc import Connection, Device, Sequence, VenusError

from pcr_resources.venus_resources.__layout__ngsstarv_deck import Ngsstarv_deck
from pcr_resources.venus_resources.__smt__ml_star import Ml_star
from pcr_resources.venus_resources.__smt__channels1ml_8 import Channels1ml_8

CH1 = '10000000'


def main() -> int:
    print("Connecting to Venus...")
    with Connection(layout_file=Ngsstarv_deck.file) as con:
        device = Device.from_existing(con)
        smt_star = Ml_star(con)
        smt_ch = Channels1ml_8(con)

        tips = Sequence(con, Ngsstarv_deck.Sequences.All_50ul_tips, deck_sequence=True)

        print("Initializing STAR...")
        smt_star.initialize(device)

        # First pickup — should succeed.
        print(f"Pickup #1 (ch1, sequence cursor=1, no increment). end={tips.end}")
        smt_ch.tip_pickup(
            ML_STAR=device,
            positions=tips,
            channel_pattern=CH1,
            increment_sequence=0,
        )

        # Drive cursor past the end of the sequence; STAR should refuse.
        tips.set_current(tips.end + 1)
        print(f"Pickup #2 (cursor={tips.current} > end={tips.end} — expect VenusError)...")
        try:
            smt_ch.tip_pickup(
                ML_STAR=device,
                positions=tips,
                channel_pattern=CH1,
                increment_sequence=0,
            )
        except VenusError as exc:
            print(f"\n*** Caught VenusError as expected ***")
            print(f"    id          : {exc.error_id}")
            print(f"    description : {exc.description}")
            print(f"    data        : {exc.data}")
            return 0

        print("\nUNEXPECTED: second pickup did not raise.")
        return 1


_VENUS_LOG_DIR = Path(r"C:\Program Files (x86)\HAMILTON\LogFiles")


def _dump_latest_trace() -> None:
    trc = sorted(
        _VENUS_LOG_DIR.glob("venusrpc_host_*_Trace.trc"),
        key=lambda f: f.stat().st_mtime,
    )
    if not trc:
        return
    latest = trc[-1]
    print(f"\n{'='*60}\n[trace] {latest.name}\n{'='*60}")
    text = latest.read_text(encoding="utf-8", errors="replace")
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


if __name__ == "__main__":
    t0 = time.monotonic()
    rc = 1
    try:
        rc = main()
    finally:
        _dump_latest_trace()
        print(f"\nelapsed: {time.monotonic() - t0:.1f}s")
    sys.exit(rc)
