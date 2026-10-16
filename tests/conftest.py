"""Generate the gitignored PCR venus_resources before test modules import them."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("run_codegen.py")), run_name="__main__")
