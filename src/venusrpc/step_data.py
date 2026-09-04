r"""Step data for device steps called from an injected command file.

A Venus device step (``ML_STAR._<CLSID>("<step-guid>")``) carries no arguments
of its own: everything it does is looked up by that guid in the *step data* of
the file the call appears in -- ``foo.hsl`` is paired with ``foo.stp``. Venus
applies the same rule to venusrpc's runtime-loaded command file, so a step
called from there needs a ``__venusrpc_cmd_N.stp`` beside it, which is what
``Connection.execute(step_data=...)`` writes.

The blocks themselves are copied out of Hamilton's own libraries at runtime
rather than vendored: they are a hundred lines of error/recovery structure per
step, and a hand-trimmed copy is a silent behaviour change on the bench.
"""

from __future__ import annotations

from pathlib import Path

LIBRARY_DIR = Path(r"C:\Program Files (x86)\HAMILTON\Library")

_HEADER = ("HxCfgFile,3;", "", "ConfigIsValid,Y;", "")


class StepDataError(LookupError):
    """A step guid is not in the .stp file it was expected in."""


def read(guid: str, stp: str | Path) -> str:
    """Return the ``DataDef`` block for *guid*, as a complete .stp document.

    *stp* is a path, or a bare file name resolved against Venus's Library
    directory (``HslStarLineMaintMetLib.stp``, say).
    """
    path = Path(stp)
    if not path.is_absolute():
        path = LIBRARY_DIR / path
    # The vendor files are CRLF; splitlines() normalises so the search does not
    # depend on which one we got, and the join puts CRLF back.
    lines = path.read_text(encoding="latin-1").splitlines()
    opening = f"DataDef,HxPars,3,{guid},"
    try:
        start = lines.index(opening)
    except ValueError as exc:
        raise StepDataError(f"step {guid} is not in {path}") from exc
    for end, line in enumerate(lines[start:], start):
        # Blocks close with a line that is exactly "];" -- the nested error and
        # recovery structures inside close with ")" or ")," and never that.
        if line.strip() == "];":
            return "\r\n".join([*_HEADER, *lines[start:end + 1], ""])
    raise StepDataError(f"step {guid} in {path} has no closing '];'")
