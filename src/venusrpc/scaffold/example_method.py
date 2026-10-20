"""Example venusrpc method: one tip, one transfer.

Setup:
    1. Copy your deck layout to ``lay/deck.lay`` (with its ``.res``) and your
       submethod libraries to ``smt/``. This example assumes an ``ml_star``
       library (initialize) and a ``channels1mL_8`` library (1 mL channels:
       tip_pickup, aspirate, dispense, tip_eject).
    2. ``venusrpc generate`` writes typed wrappers into ``venus_resources/``.
    3. Rename the ``Deck.Sequences`` members below to your deck's sequences
       (your editor autocompletes them), then ``python example_method.py``.

Submethod parameters you leave out take the defaults set in the ``.smt``.

Runs in Venus simulation mode until you set ``SIMULATION = False``.
"""

from venusrpc import Connection, Device, Sequence
from venus_resources.__layout__deck import Deck
from venus_resources.__smt__channels1ml_8 import Channels1ml_8
from venus_resources.__smt__ml_star import Ml_star

SIMULATION = True
LIQUID_CLASS = "Tip_50ulFilter_Water_DispenseSurface_Empty"
CHANNELS = "10000000"  # channel 1 only; "11111111" for all eight
VOLUME_UL = 10


def main() -> None:
    with Connection(layout_file=Deck.file, simulation=SIMULATION) as con:
        star = Device.from_existing(con)
        channels = Channels1ml_8(con)

        tips = Sequence(con, Deck.Sequences.Tips_50ul, deck_sequence=True)
        source = Sequence(con, Deck.Sequences.Source_tubes, deck_sequence=True)
        target = Sequence(con, Deck.Sequences.Target_plate, deck_sequence=True)

        Ml_star(con).initialize(star)

        channels.tip_pickup(ML_STAR=star, positions=tips, channel_pattern=CHANNELS)
        channels.aspirate(
            ML_STAR=star,
            positions=source,
            volume=VOLUME_UL,
            liquid_class=LIQUID_CLASS,
            channel_pattern=CHANNELS,
        )
        channels.dispense(
            ML_STAR=star,
            positions=target,
            volume=VOLUME_UL,
            channel_pattern=CHANNELS,
            cLLD_sensitivity=0,  # no liquid level detection in an empty well
            fixed_height=2,  # mm above the well bottom
        )
        channels.tip_eject(
            ML_STAR=star, positions=tips, default_waste=1, channel_pattern=CHANNELS
        )

        # Sequences advance as Venus consumes positions; read the cursor back.
        print(f"next tip: {tips.current}, next target well: {target.current}")


if __name__ == "__main__":
    main()
