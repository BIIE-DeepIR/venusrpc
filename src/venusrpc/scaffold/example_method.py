"""Minimal venusrpc method skeleton.

Run with:
    python example_method.py
"""

from venusrpc import Connection, Device, Sequence, Variable


def main() -> None:
    with Connection() as con:
        # star = Device(con, r"C:\path\to\your\layout.lay")
        # tips = Sequence(con, name="tips", deck_sequence=True)
        # volume = Variable(con, 100, name="volume_ul")
        # ... call generated submethod wrappers ...
        result = con.execute('AddJsonVariable(___JSON___, "ok", "status");')
        print(result)


if __name__ == "__main__":
    main()
