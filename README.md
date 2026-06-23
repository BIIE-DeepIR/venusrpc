# venusrpc

Fast HTTP-based Python bridge to the Hamilton Venus HSL runtime.

`venusrpc` lets you drive a Hamilton STAR liquid handler from Python by calling
HSL submethods over a loopback HTTP connection to the Venus run-time
environment (`HxRun.exe`). The HSL host script that runs inside Venus is
shipped with the package.

## Credit

`venusrpc` is a separate implementation of the idea pioneered by
[PyVenus](https://github.com/sniprbiome/PyVenus) by SNIPR Biome — driving
Venus from Python via an HSL host script. The codegen approach (parsing
`.lay` / `.smt` / `.hsi` to emit typed Python wrappers) and the HSL helpers
shipped here are directly inspired by their work. `venusrpc` swaps PyVenus's
file-polling IPC for a loopback HTTP socket so per-call latency drops from
~150–300 ms to ~1–5 ms.

## Status

Alpha. Transport layer is complete but the HSL host script contains TODOs that
must be reconciled against your local `HSLTcpIpLib.chm` before the runtime
side can be loaded by Venus. See
`src/venusrpc/hsl/venusrpc_host.hsl` for the markers.

## Install

```bash
uv pip install venusrpc          # core (includes pyodbc)
uv pip install 'venusrpc[dev]'   # tests + linting
```

## Quickstart

```python
from venusrpc import Connection, Device, Variable, Sequence

with Connection() as con:
    star = Device(con, r"C:\Path\to\layout.lay")

    volume = Variable(con, 100, name="volume_ul")
    tips = Sequence(con, name="tips", deck_sequence=True)

    # ...call a generated submethod wrapper here...
    print(con.execute('AddJsonVariable(___JSON___, volume_ul, "v");'))
```

`Connection.execute()` returns a parsed `dict`, not a JSON string.

## How it works

```
Python (HTTP server)                   Venus (HTTP client)
─────────────────                      ─────────────────
HTTPServer on 127.0.0.1:0  ◄── GET ─── HxRun.exe -t venusrpc_host.hsl
write venusrpc.port
─── HTTP response (JSON) ─────────►   __EvalExpr__()
◄── next GET with result    ──────
... loop ...
```

Python is the server because it is the only side that knows its ephemeral
port. The HSL host reads `venusrpc.port` from the same directory as the host
script and connects back.

## Sequence pull semantics

`Sequence.pull()` (called automatically at the end of every generated SMT
wrapper) returns only the cursor — `current` and `end`. Labware and position
entries are **not** round-tripped: Python pushed them, Venus's
`increment_sequence` only walks the cursor along them, so re-serializing the
arrays on every call wastes bandwidth and exposes an HSL `sequence` quirk
where each `push()` appends to `GetTotal()` without truncating, so the JSON
payload doubles per call (~30 bytes → MB after a dozen iterations, killing
HxRun via stack/memory pressure).

| Call | Returns | Use |
|--|--|--|
| `seq.pull()` | `current`, `end` | default; safe for any SMT op |
| `seq.pull_full()` | `current`, `end`, `labware`, `position` | rare: Venus populated the sequence itself (e.g. a barcode scan SMT) |
| `seq.pull_with_xyz()` | + per-position xyz/angle | inspecting taught coordinates |

Automatic sequence counting still works — Venus's `increment_sequence`
advances `GetCurrentPosition()`, which `pull()` reads back. The only thing
you lose is the ability to see labware/position changes Venus made
server-side, which no current SMT does. If you ever write one that mutates a
sequence, call `pull_full()` at that one site.

## Codegen

`venusrpc.Codegen` parses Venus deck layouts (`.lay`), submethod libraries
(`.smt` + `.hsi`), labware definitions (`.rck`/`.tml`/`.lid` + linked `.ctr`),
and (optionally) the Hamilton liquid class Access DB. It emits typed Python
wrappers into `./venus_resources/` that import from `venusrpc`.

```python
from venusrpc import Codegen
Codegen.read_layout("example_layout.lay")
Codegen.read_submethods("./smt")
Codegen.read_liquid_classes()  # Windows + pyodbc
Codegen.read_labware("example_layout.lay")  # vendors + parses referenced labware
```

`read_labware` resolves every placed labware in the `.lay` to its definition
file, copies (vendors) the referenced files under `resources/labware/` so the
build is repo-local, and emits `__labware__.py` — one **top-level class per
definition**, each subclassing `Rack`, `Carrier`, or `Lid` (from `venusrpc`) so
its kind is explicit. Every definition carries footprint `length`/`width`/
`height`; `Rack`s additionally expose `rows`/`columns`/pitch, well
`depth`/`max_volume`/`volume_eqn`, and the `segments_x`/`segments_y` profile
(with `width_at_x(h)`/`width_at_y(h)` helpers).

Placed instances live on the **deck layout class**, not in `__labware__`: when
labware codegen is enabled, the deck class gains an `Instances` nested class of
`LabwareInstance` objects — each carrying the **exact placed name** (`.name`,
e.g. `"Cos_96_HalfArea_PR_Source"`) and its shared `.definition`. Other
attributes proxy to the definition, so no data is duplicated:

```python
inst = Ngsstarv_deck.Instances.Cos_96_HalfArea_PR_Source
inst.name        # "Cos_96_HalfArea_PR_Source"  (exact deck Id)
inst.definition  # labware.Cos_96_HalfArea       (shared Rack subclass)
inst.model       # "Cos_96_HalfArea"             (proxied; the .rck stem)
inst.length      # 127.0                          (proxied)
```

The definition's own type name is `model` (the placed name owns `name`).

### Config-driven codegen

Instead of calling `Codegen` by hand on every change, declare a
`[tool.venusrpc]` block in `pyproject.toml` (or a standalone `venusrpc.toml`
in the project root) and use the `venusrpc` CLI.

```toml
# pyproject.toml
[tool.venusrpc]
layouts = ["lay/NGSSTARV_Deck.lay"]   # str or list; paths relative to this file
submethods_dir = "smt"                 # default: ./smt
output_dir = "venus_resources"         # default: ./venus_resources

# Opt-in runtime guard. Set to `true` (strict, raises StaleResourcesError),
# `"warn"` (issue UserWarning), or omit/false to skip. See verify_resources().
verify_on_import = false

[tool.venusrpc.liquid_classes]
enabled = true                         # set false to skip the DB scan
include_1ml_channels = true
include_5ml_channels = false
include_96head = true
include_384head = false
include_washstations = false
include_default = true
include_custom = true
database = 'C:\Program Files (x86)\HAMILTON\Config\ML_STARLiquids.mdb'

[tool.venusrpc.labware]
enabled = true                         # set false to skip labware codegen
# Source root the deck's relative labware paths resolve against. Referenced
# definitions are vendored into vendor_dir (repo-local) and parsed from there.
base = 'C:\Program Files (x86)\HAMILTON\LabWare'
vendor_dir = "resources/labware"       # default: resources/labware (rel. to this file)
```

Commands (auto-detect config in cwd; override with `--config <file>`):

| Command | Behavior |
|---------|----------|
| `venusrpc generate` | Regenerate wrappers whose source hash differs. Up-to-date files print `Up-to-date: <path>`. Regenerated print `Generated: <path>`. |
| `venusrpc generate --force` | Regenerate every wrapper regardless of hash. Use after editing the codegen template. |
| `venusrpc check` | Verify every generated file's stored hash matches its source. Exits **0** if clean, **2** if any entry is stale or missing — suitable for CI / pre-commit. |
| `venusrpc check --warn` | Same diagnostic, but exits 0. |

### Opt-in runtime guard

For an early failure when a teammate forgets to run `venusrpc generate`,
import the helper at the top of your driver script:

```python
from venusrpc import verify_resources
verify_resources()   # honors `verify_on_import` from config

# or force a mode regardless of config:
verify_resources(mode="strict")  # raises StaleResourcesError
verify_resources(mode="warn")    # emits UserWarning
```

`mode="auto"` (default) reads `verify_on_import` from the resolved config:

| `verify_on_import` value | Effect |
|--------------------------|--------|
| `true` | strict — raises `StaleResourcesError` listing every stale entry |
| `"warn"` | issues a `UserWarning`, returns the stale list |
| `false` / unset | no-op |

## Differences from PyVenus

| | PyVenus | venusrpc |
|--|---------|----------|
| Transport | File polling (100 ms / 200 ms loops) | HTTP over loopback, blocking I/O |
| Per-call floor latency | ~150–300 ms | ~1–5 ms |
| `Connection.execute()` returns | JSON string | parsed `dict` |
| Re-parsing call sites | required | not needed |
| Liquid class DB driver | hard dependency on `pyodbc` | hard dependency on `pyodbc` |
| Python | 3.8+ | 3.10+ |

## Development

```bash
uv sync --extra dev
uv run pytest
```

## License

LGPL-3.0-or-later.
