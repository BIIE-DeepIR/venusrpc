"""Code generation from Hamilton Venus resources.

Inputs the user supplies:
    * Deck layout files (.lay)
    * Submethod libraries (.smt + .hsi + .hs_)
    * Optional liquid class database (.mdb) — Windows-only, requires pyodbc.

Outputs (under ``./venus_resources/`` by default) are Python modules that
import from ``venusrpc`` and expose typed classes the user can call from
their scripts.
"""

from __future__ import annotations

import hashlib
import json
import pprint
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

_HX_CFG_CONVERTER = Path(r"C:\Program Files (x86)\HAMILTON\Bin\HxCfgFilConverter.exe")

_SUBMETHOD_HASH_PREFIX = "# venusrpc-submethod-hash: "
_LAYOUT_HASH_PREFIX = "# venusrpc-layout-hash: "
_LABWARE_HASH_PREFIX = "# venusrpc-labware-hash: "
_SUBMETHOD_CODEGEN_SALT = b"submethod-codegen-v2"
_LABWARE_CODEGEN_SALT = b"labware-codegen-v1"

_LABWARE_BASE_DEFAULT = Path(r"C:\Program Files (x86)\HAMILTON\LabWare")

# Source-file extension -> venusrpc base class for the generated definition.
_LABWARE_BASE_CLASSES = {"rck": "Rack", "tml": "Carrier", "lid": "Lid"}


def _sanitize_identifier(raw: str) -> str:
    cleaned: list[str] = []
    first = True
    for ch in raw:
        ok = ch == "_" or (ch.isalpha() if first else (ch.isalnum() or ch == "_"))
        if ok:
            cleaned.append(ch)
            first = False
    return "".join(cleaned)


def _sanitize_attr(raw: str) -> str:
    """Make a valid Python attr name, preserving digits via leading underscore."""
    out = [ch if (ch.isalnum() or ch == "_") else "_" for ch in raw]
    s = "".join(out)
    if not s:
        return "_"
    if s[0].isdigit():
        s = "_" + s
    return s


def _hex_replace(m: re.Match[str]) -> str:
    return chr(int(m.group(1), 16))


def _python_default_literal(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return repr(raw[1:-1])
    try:
        float(raw)
        return raw
    except ValueError:
        return repr(raw)


_FLAT_KV_RE = re.compile(r'^([A-Za-z0-9_.]+),\s*"(.*)",\s*$', re.MULTILINE)


def _parse_flat_hxcfg(text: str) -> dict[str, str]:
    """Parse a flat ``Key, "Value",`` HxCfg file (.rck/.ctr/.tml/.lid)."""
    return {m.group(1): m.group(2) for m in _FLAT_KV_RE.finditer(text)}


def _flat_float(d: dict[str, str], key: str) -> float | None:
    raw = d.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _flat_int(d: dict[str, str], key: str) -> int | None:
    f = _flat_float(d, key)
    return int(f) if f is not None else None


def _safe_volume(eqn: str | None, height: float | None) -> float | None:
    """Evaluate a Hamilton volume equation ``EqnOfVol`` at a liquid height.

    Equations are simple arithmetic in ``h`` (e.g. ``"h*17.7369"``). Only a
    whitelisted character set is evaluated, with no builtins available.
    """
    if not eqn or height is None:
        return None
    expr = eqn.replace("^", "**")
    if not re.fullmatch(r"[0-9.eE+\-*/() h]+", expr):
        return None
    try:
        value = eval(expr, {"__builtins__": {}}, {"h": height})  # noqa: S307
    except Exception:
        return None
    return round(float(value), 4) if isinstance(value, (int, float)) else None


def _convert_to_ascii(file: Path) -> None:
    """Run Hamilton's binary->ASCII converter on a file in place."""
    if not _HX_CFG_CONVERTER.exists():
        return
    subprocess.run(
        [str(_HX_CFG_CONVERTER), "/t", str(file)],
        check=False,
        stdout=subprocess.PIPE,
        text=True,
    )


def _read_ascii(source: Path) -> str:
    """Return ASCII text of an HxCfg file without mutating the original.

    Copies into a temp dir, runs the converter on the copy, reads, discards.
    """
    if not source.exists():
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / source.name
        shutil.copy2(source, scratch)
        _convert_to_ascii(scratch)
        return scratch.read_text(encoding="utf-8", errors="replace")


@dataclass
class _Parameter:
    name: str
    hsl_type: str
    py_type: str
    is_array: bool
    direction: str  # in | out | in_out
    pass_mode: str  # variable | value
    comment: str
    default_value: str | None = None


@dataclass
class _Function:
    name: str
    has_return: bool
    parameters: list[_Parameter]  # Python order: required first, optional second
    comment: str
    hsl_parameters: list[_Parameter] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.hsl_parameters:
            self.hsl_parameters = list(self.parameters)


@dataclass
class _Submethod:
    name: str
    file: str
    functions: list[_Function] = field(default_factory=list)


class Codegen:
    """Entry points for generating Python wrappers from Venus resources."""

    @classmethod
    def read_layout(
        cls,
        layout: str | Path,
        output_dir: str | Path | None = None,
        *,
        labware_module: str | None = None,
        force: bool = False,
    ) -> Path:
        """Generate a Python module from a .lay deck layout.

        If ``labware_module`` is given (e.g. ``"__labware__"``), the deck class
        also gets an ``Instances`` nested class mapping each placed labware name
        to its definition class in that sibling module.

        Returns the path to the generated .py file.
        """
        layout = Path(layout).resolve()
        out_dir = cls._resolve_output_dir(output_dir)
        layout_stem = layout.stem
        sanitized = _sanitize_identifier(layout_stem.lower())
        generated_stem = f"__layout__{sanitized}"
        generated_py = out_dir / f"{generated_stem}.py"
        res_source = layout.with_suffix(".res")

        new_hash = cls._layout_hash(layout, res_source)
        if not force and cls._read_hash(generated_py, _LAYOUT_HASH_PREFIX) == new_hash:
            print(f"Up-to-date: {generated_py}")
            return generated_py

        content = _read_ascii(layout)
        sequences = re.findall(r'Seq\.\d+\.Name,\s*"([A-Za-z0-9_]+)",', content)
        labware = re.findall(r'Labware\.\d+\.Id,\s*"([A-Za-z0-9_]+)",', content)

        # (var, definition_attr, name_literal_or_None). var is the Python
        # attribute; name_literal is set only when the exact placed name differs
        # from var (LabwareInstance.__set_name__ recovers it otherwise).
        instances: list[tuple[str, str, str | None]] = []
        if labware_module:
            placements, attr_by_rel = cls._labware_index(layout)
            seen: dict[str, str] = {}  # var -> exact instance name
            for inst, relpath in placements:
                attr = attr_by_rel.get(relpath)
                if attr is None:
                    continue
                var = _sanitize_attr(inst)
                base_var, n = var, 2
                while var in seen and seen[var] != inst:
                    var = f"{base_var}_{n}"
                    n += 1
                if var in seen:  # duplicate placement of the same instance id
                    continue
                seen[var] = inst
                name_lit = None if var == inst else repr(inst)
                instances.append((var, attr, name_lit))
            instances.sort(key=lambda t: t[0].lower())

        env = cls._jinja_env()
        template = env.get_template("deck_layout.py.j2")
        body = template.render(
            layout_file=str(layout).replace("\\", "\\\\"),
            layout_name=sanitized,
            sequences=sorted(set(sequences)),
            labware=sorted(set(labware)),
            labware_module=labware_module,
            instances=instances,
        )
        generated_py.write_text(
            f"{_LAYOUT_HASH_PREFIX}{new_hash}\n{body}", encoding="utf-8"
        )
        print(f"Generated: {generated_py}")
        cls._regenerate_init(out_dir)
        return generated_py

    @classmethod
    def read_submethods(
        cls,
        directory: str | Path | None = None,
        output_dir: str | Path | None = None,
        *,
        force: bool = False,
    ) -> list[Path]:
        """Generate Python wrappers for every .smt/.hsi pair under ``directory``.

        Default ``directory`` is ``./smt``.
        """
        in_dir = Path(directory) if directory else Path(sys.path[0]) / "smt"
        in_dir = in_dir.resolve()
        out_dir = cls._resolve_output_dir(output_dir)

        generated: list[Path] = []
        for hsi_path in in_dir.rglob("*.hsi"):
            if hsi_path.stem.startswith("~"):
                continue
            smt_source = hsi_path.with_suffix(".smt")
            if not smt_source.exists():
                continue

            sanitized = _sanitize_identifier(hsi_path.stem.replace(" ", "_"))
            generated_py = out_dir / f"__smt__{sanitized}.py".lower()
            new_hash = cls._submethod_hash(smt_source, hsi_path)
            if not force and cls._read_submethod_hash(generated_py) == new_hash:
                print(f"Up-to-date: {generated_py}")
                continue

            submethod = cls._parse_submethod(
                hsi_path=hsi_path,
                smt_source=smt_source,
            )
            if submethod is None:
                continue

            generated.append(cls._render_submethod(submethod, out_dir, new_hash))

        cls._regenerate_init(out_dir)
        return generated

    @classmethod
    def read_liquid_classes(
        cls,
        *,
        include_1ml_channels: bool = True,
        include_5ml_channels: bool = False,
        include_96head: bool = True,
        include_384head: bool = False,
        include_washstations: bool = False,
        include_default: bool = True,
        include_custom: bool = True,
        database: str
        | Path = r"C:\Program Files (x86)\HAMILTON\Config\ML_STARLiquids.mdb",
        output_dir: str | Path | None = None,
    ) -> Path:
        """Generate a Python module wrapping the Hamilton liquid class DB.

        Requires a Microsoft Access ODBC driver. Windows-only.
        """
        import pyodbc

        out_dir = cls._resolve_output_dir(output_dir)
        database = Path(database)

        drivers = [
            "Microsoft Access Driver (*.mdb, *.accdb)",
            "Microsoft Access Driver (*.mdb)",
            "Driver do Microsoft Access (*.mdb)",
            "Microsoft Access-Treiber (*.mdb)",
        ]
        driver = next((d for d in drivers if d in pyodbc.drivers()), None)
        if driver is None:
            raise RuntimeError(
                "No Microsoft Access ODBC driver installed; install the "
                "Microsoft Access Database Engine redistributable."
            )

        conn = pyodbc.connect(f"Driver={{{driver}}};DBQ={database};")
        try:
            cursor = conn.cursor()
            cursor.execute("select * from LiquidClass")
            columns = [c[0] for c in cursor.description]
            selected_devices = set()
            if include_1ml_channels:
                selected_devices.add(1)
            if include_5ml_channels:
                selected_devices.add(7)
            if include_96head:
                selected_devices.add(2)
            if include_384head:
                selected_devices.add(8)
            if include_washstations:
                selected_devices.update({3, 4, 5, 6, 9})
            selected_types = set()
            if include_default:
                selected_types.add(1)
            if include_custom:
                selected_types.add(0)

            classes: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                devices = {int(x) for x in str(row[4]).split(";") if x.strip()}
                if not devices & selected_devices:
                    continue
                if row[8] in (6, 7, 8):
                    continue
                if row[1] in ("a", "abc", "abcd", "1"):
                    continue
                if row[7] in (0, 1):
                    continue
                if row[6] not in selected_types:
                    continue
                params = dict(zip(columns, row))
                for k in ("LiquidNotes", "CorrectionCurve", "DesignHistory"):
                    params.pop(k, None)
                params = {k: params[k] for k in sorted(params)}
                classes.append(
                    {
                        "attr": _sanitize_attr(row[1]),
                        "name": row[1],
                        "parameters_repr": pprint.pformat(
                            params, width=100, sort_dicts=False
                        ),
                    }
                )
        finally:
            conn.close()

        env = cls._jinja_env()
        template = env.get_template("liquid_classes.py.j2")
        out_file = out_dir / "__liquid_classes__.py"
        out_file.write_text(template.render(liquid_classes=classes), encoding="utf-8")
        print(f"Generated: {out_file}")
        cls._regenerate_init(out_dir)
        return out_file

    @classmethod
    def read_labware(
        cls,
        layout: str | Path,
        output_dir: str | Path | None = None,
        *,
        labware_base: str | Path = _LABWARE_BASE_DEFAULT,
        vendor_dir: str | Path | None = None,
        repo_root: str | Path | None = None,
        force: bool = False,
    ) -> Path:
        """Generate a Python module of labware properties from a deck layout.

        Resolves every placed labware in the ``.lay`` to its definition file,
        copies (vendors) the referenced ``.rck``/``.tml``/``.lid`` (and linked
        ``.ctr``) under ``vendor_dir`` so the build is repo-local, then parses
        them into a definition-keyed ``__labware__.py``.

        Returns the path to the generated .py file.
        """
        layout = Path(layout).resolve()
        out_dir = cls._resolve_output_dir(output_dir)
        base = Path(labware_base)
        vendor = (
            Path(vendor_dir).resolve()
            if vendor_dir
            else (out_dir.parent / "resources" / "labware")
        )
        root = Path(repo_root).resolve() if repo_root else vendor.parent.parent
        generated_py = out_dir / "__labware__.py"

        placements, attr_by_rel = cls._labware_index(layout)
        if not placements:
            print(f"No labware found in layout: {layout}")
            return generated_py

        # Vendor every referenced definition (+ its linked .ctr) into the repo.
        vendored: dict[str, Path] = {}  # relpath -> vendored definition path
        ctr_for: dict[str, Path | None] = {}  # relpath -> vendored/source .ctr
        for relpath in sorted(attr_by_rel):
            dst = cls._vendor_file(base, relpath, vendor, force=force)
            if dst is None:
                continue
            vendored[relpath] = dst
            ctr_for[relpath] = cls._vendor_linked_ctr(base, relpath, dst, force=force)

        hash_inputs = list(vendored.values()) + [p for p in ctr_for.values() if p]
        new_hash = cls._labware_hash(layout, hash_inputs)
        if not force and cls._read_hash(generated_py, _LABWARE_HASH_PREFIX) == new_hash:
            print(f"Up-to-date: {generated_py}")
            return generated_py

        defs: list[dict[str, Any]] = []
        for relpath in sorted(vendored):
            def_path = vendored[relpath]
            kind = def_path.suffix.lstrip(".").lower()
            defs.append(
                cls._build_labware_entry(
                    attr=attr_by_rel[relpath],
                    def_path=def_path,
                    ctr_path=ctr_for.get(relpath),
                    kind=kind,
                    repo_root=root,
                )
            )
        defs.sort(key=lambda e: e["attr"].lower())

        bases = sorted({e["base"] for e in defs})

        env = cls._jinja_env()
        template = env.get_template("labware.py.j2")
        body = template.render(defs=defs, bases=bases)
        generated_py.write_text(
            f"{_LABWARE_HASH_PREFIX}{new_hash}\n{body}", encoding="utf-8"
        )
        print(f"Generated: {generated_py} ({len(defs)} definitions)")
        cls._regenerate_init(out_dir)
        return generated_py

    @classmethod
    def _parse_lay_labware(cls, layout: Path) -> list[tuple[str, str]]:
        """Return (placed_instance_id, definition_relpath) pairs from a .lay."""
        content = _read_ascii(layout)
        ids = dict(re.findall(r'Labware\.(\d+)\.Id,\s*"([^"]*)"', content))
        files = dict(re.findall(r'Labware\.(\d+)\.File,\s*"([^"]*)"', content))
        out: list[tuple[str, str]] = []
        for n, inst in ids.items():
            f = files.get(n)
            if not inst or not f:
                continue
            out.append((inst, f.replace("\\\\", "\\")))
        return out

    @classmethod
    def _labware_index(
        cls, layout: Path
    ) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """Shared labware naming used by both labware and layout codegen.

        Returns ``(placements, attr_by_relpath)`` where ``placements`` is the
        list of ``(instance_id, relpath)`` pairs and ``attr_by_relpath`` maps
        each unique definition path to its (collision-resolved) class-attr
        name. read_labware names definition classes from this map; read_layout
        references those same names from the deck's ``Instances`` — so they
        always match.
        """
        placements = cls._parse_lay_labware(layout)
        attr_by_rel: dict[str, str] = {}
        used: dict[str, str] = {}  # attr -> relpath
        for relpath in sorted({rel for _, rel in placements}):
            p = Path(relpath)
            stem, kind = p.stem, p.suffix.lstrip(".").lower()
            attr = _sanitize_attr(stem)
            if attr in used and used[attr] != relpath:
                attr = _sanitize_attr(f"{stem}_{kind}")
            base_attr, n = attr, 2
            while attr in used and used[attr] != relpath:
                attr = f"{base_attr}_{n}"
                n += 1
            used[attr] = relpath
            attr_by_rel[relpath] = attr
        return placements, attr_by_rel

    @staticmethod
    def _vendor_file(
        base: Path, relpath: str, vendor_dir: Path, *, force: bool
    ) -> Path | None:
        """Copy ``base/relpath`` into ``vendor_dir/relpath`` (idempotent)."""
        src = base / relpath
        if not src.exists():
            warnings.warn(f"labware definition not found, skipping: {src}")
            return None
        dst = vendor_dir / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        if force or not dst.exists():
            shutil.copy2(src, dst)
        return dst

    @classmethod
    def _vendor_linked_ctr(
        cls, base: Path, relpath: str, vendored_def: Path, *, force: bool
    ) -> Path | None:
        """Resolve a definition's ``Cntr.1`` well file and vendor it alongside."""
        d = _parse_flat_hxcfg(_read_ascii(vendored_def))
        rel = (d.get("Cntr.1.fileRel") or "").replace("\\", "/")
        absf = (d.get("Cntr.1.file") or "").replace("\\", "/")
        src_dir = (base / relpath).parent
        candidates: list[Path] = []
        if rel:
            candidates.append((src_dir / rel))
        if absf:
            p = Path(absf)
            candidates.append(p if p.is_absolute() else base / absf)
        src_ctr: Path | None = None
        for c in candidates:
            try:
                if c.exists():
                    src_ctr = c.resolve()
                    break
            except OSError:
                continue
        if src_ctr is None:
            return None
        dst = vendored_def.parent / src_ctr.name
        if force or not dst.exists():
            shutil.copy2(src_ctr, dst)
        return dst

    @classmethod
    def _build_labware_entry(
        cls,
        *,
        attr: str,
        def_path: Path,
        ctr_path: Path | None,
        kind: str,
        repo_root: Path,
    ) -> dict[str, Any]:
        """Parse one labware definition (tier-by-presence) into template data.

        ``kind`` (file extension) picks the base class; the kind itself is
        carried by that base class, so it is not re-emitted on the subclass.
        """
        d = _parse_flat_hxcfg(_read_ascii(def_path))
        try:
            rel_file = def_path.relative_to(repo_root).as_posix()
        except ValueError:
            rel_file = def_path.as_posix()

        base = _LABWARE_BASE_CLASSES.get(kind, "LabwareDef")
        lines: list[tuple[str, str]] = [
            ("model", repr(def_path.stem)),
            ("file", repr(rel_file)),
            ("length", repr(_flat_float(d, "Dim.Dx"))),
            ("width", repr(_flat_float(d, "Dim.Dy"))),
            ("height", repr(_flat_float(d, "Dim.Dz"))),
            ("stack_height", repr(_flat_float(d, "StackHt"))),
        ]

        rows = _flat_int(d, "Rows")
        columns = _flat_int(d, "Columns")
        if rows is not None and columns is not None:
            lines += [
                ("rows", repr(rows)),
                ("columns", repr(columns)),
                ("pitch_x", repr(_flat_float(d, "Dx"))),
                ("pitch_y", repr(_flat_float(d, "Dy"))),
            ]

        if ctr_path is not None:
            cd = _parse_flat_hxcfg(_read_ascii(ctr_path))
            depth = _flat_float(cd, "Depth")
            max_depth = _flat_float(cd, "1.Max")
            eqn = cd.get("1.EqnOfVol") or None
            lines += [
                ("well_depth", repr(depth)),
                ("well_max_depth", repr(max_depth)),
                ("well_max_volume", repr(_safe_volume(eqn, max_depth))),
                ("volume_eqn", repr(eqn)),
            ]

        segs_x = cls._segments(d, "x")
        segs_y = cls._segments(d, "y")
        if segs_x:
            lines.append(("segments_x", repr(segs_x)))
        if segs_y:
            lines.append(("segments_y", repr(segs_y)))

        return {
            "attr": attr,
            "base": base,
            "doc": f"{def_path.stem} ({kind})",
            "lines": lines,
        }

    @staticmethod
    def _segments(d: dict[str, str], axis: str) -> tuple[tuple[float | None, ...], ...]:
        """Extract the ``Seg_<axis>`` footprint profile, bottom-up."""
        count = _flat_int(d, f"SegmentCount_{axis}") or 0
        segs: list[tuple[float | None, float | None, float | None]] = []
        for i in range(count):
            lower = _flat_float(d, f"Seg_{axis}.{i}.LowerWidth")
            upper = _flat_float(d, f"Seg_{axis}.{i}.UpperWidth")
            seg_h = _flat_float(d, f"Seg_{axis}.{i}.SegmentHeight")
            if lower is None and upper is None:
                continue
            segs.append((lower, upper, seg_h))
        return tuple(segs)

    @classmethod
    def _labware_hash(cls, layout: Path, vendored_paths: list[Path]) -> str:
        env = cls._jinja_env()
        template_src, _, _ = env.loader.get_source(env, "labware.py.j2")
        h = hashlib.sha256()
        h.update(layout.read_bytes())
        h.update(b"\0")
        for p in sorted(vendored_paths, key=lambda x: str(x).lower()):
            try:
                h.update(p.read_bytes())
            except OSError:
                pass
            h.update(b"\0")
        h.update(template_src.encode("utf-8"))
        h.update(b"\0")
        h.update(_LABWARE_CODEGEN_SALT)
        return h.hexdigest()

    @classmethod
    def verify(
        cls,
        *,
        layouts: list[str | Path] | None = None,
        submethods_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> list[tuple[Path, str]]:
        """Check generated wrappers are in sync with their sources.

        Returns a list of (source_or_target_path, reason) tuples for every
        stale entry. Empty list means everything is current.
        """
        stale: list[tuple[Path, str]] = []
        out_dir = cls._resolve_output_dir(output_dir)

        for lay in layouts or []:
            lay_path = Path(lay).resolve()
            sanitized = _sanitize_identifier(lay_path.stem.lower())
            generated_py = out_dir / f"__layout__{sanitized}.py"
            res_source = lay_path.with_suffix(".res")
            want = cls._layout_hash(lay_path, res_source)
            got = cls._read_hash(generated_py, _LAYOUT_HASH_PREFIX)
            if got != want:
                stale.append((lay_path, "missing" if got is None else "stale"))

        if submethods_dir is not None:
            in_dir = Path(submethods_dir).resolve()
            for hsi_path in in_dir.rglob("*.hsi"):
                if hsi_path.stem.startswith("~"):
                    continue
                smt_source = hsi_path.with_suffix(".smt")
                if not smt_source.exists():
                    continue
                sanitized = _sanitize_identifier(hsi_path.stem.replace(" ", "_"))
                generated_py = out_dir / f"__smt__{sanitized}.py".lower()
                want = cls._submethod_hash(smt_source, hsi_path)
                got = cls._read_submethod_hash(generated_py)
                if got != want:
                    stale.append((hsi_path, "missing" if got is None else "stale"))

        return stale

    @classmethod
    def _resolve_output_dir(cls, output_dir: str | Path | None) -> Path:
        out = Path(output_dir) if output_dir else Path(sys.path[0]) / "venus_resources"
        out.mkdir(parents=True, exist_ok=True)
        return out

    @classmethod
    def _jinja_env(cls) -> Environment:
        return Environment(
            loader=PackageLoader("venusrpc", "templates"),
            autoescape=select_autoescape(["html", "xml"]),
            keep_trailing_newline=True,
        )

    @classmethod
    def _parse_submethod(cls, hsi_path: Path, smt_source: Path) -> _Submethod | None:
        content = hsi_path.read_text(encoding="utf-8", errors="replace")
        smt_def = cls._parse_hxcfg_source(smt_source)
        comment_lookup = cls._build_comment_lookup(smt_def)

        pattern_function = re.compile(
            r'// {{{ \d+ "([\w\d_]+)" "Begin"\n'
            r"function \1\( ([\w\d\s&,\[\]]*) \)\s(\w+)\s{\n"
            r'// }} ""\n[\w\d\s;\[\]\n]*'
            r'// {{ \d+ "\1" "InitLocals"\n'
            r'([\w\s\d\(\)&,\[\]{}\/";=\.]*?)\n?'
            r'// }} ""'
        )
        pattern_parameter = re.compile(r"(\w+)\s(&?)\s?([\w\d&]+)([\[\]]*)[,]*")
        pattern_default = re.compile(r"\{\{default:(.*?)\}\}(.*)")

        submethod = _Submethod(
            name=_sanitize_identifier(hsi_path.stem.replace(" ", "_")),
            file=str(hsi_path.with_suffix(".hs_")).replace("\\", "\\\\"),
        )

        for match in pattern_function.finditer(content):
            fname = match.group(1)
            init_locals = match.group(4)
            sub_comment, param_comments = comment_lookup.get(fname, ("", {}))

            params: list[_Parameter] = []
            skip = False
            for pm in pattern_parameter.finditer(match.group(2)):
                hsl_type = pm.group(1)
                ref_mark = pm.group(2)
                pname = pm.group(3)
                is_array = pm.group(4) == "[]"

                if pname in init_locals:
                    direction = "out"
                elif ref_mark == "&":
                    direction = "in_out"
                else:
                    direction = "in"

                if hsl_type == "variable" and is_array:
                    py_type = "Array"
                else:
                    py_type = hsl_type.capitalize()

                pass_mode = (
                    "value"
                    if py_type == "Variable" and direction == "in"
                    else "variable"
                )

                comment = param_comments.get(pname, "")
                default = None
                if comment:
                    m = pattern_default.match(comment)
                    if m:
                        default = _python_default_literal(m.group(1))
                        comment = m.group(2)

                if hsl_type == "sequence" and is_array:
                    warnings.warn(
                        f"{hsi_path.name} ({fname}): arrays of sequences are "
                        f"unsupported; skipping submethod"
                    )
                    skip = True
                    break
                if hsl_type in ("file", "timer", "event", "object", "resource"):
                    warnings.warn(
                        f"{hsi_path.name} ({fname}): HSL type {hsl_type!r} "
                        f"unsupported; skipping submethod"
                    )
                    skip = True
                    break
                if hsl_type != "variable" and default is not None:
                    warnings.warn(
                        f"{hsi_path.name} ({fname}): defaults only supported "
                        f"for variables; skipping"
                    )
                    skip = True
                    break
                if direction != "in" and default is not None:
                    warnings.warn(
                        f"{hsi_path.name} ({fname}): defaults only allowed for "
                        f"input params; skipping"
                    )
                    skip = True
                    break

                params.append(
                    _Parameter(
                        name=pname,
                        hsl_type=hsl_type,
                        py_type=py_type,
                        is_array=is_array,
                        direction=direction,
                        pass_mode=pass_mode,
                        comment=comment,
                        default_value=default,
                    )
                )

            if skip:
                continue
            hsl_params = list(params)
            params = [p for p in params if p.default_value is None] + [
                p for p in params if p.default_value is not None
            ]
            submethod.functions.append(
                _Function(
                    name=fname,
                    has_return=match.group(3) == "variable",
                    parameters=params,
                    hsl_parameters=hsl_params,
                    comment=sub_comment,
                )
            )
        return submethod

    @classmethod
    def _render_submethod(
        cls, submethod: _Submethod, out_dir: Path, file_hash: str
    ) -> Path:
        env = cls._jinja_env()
        template = env.get_template("submethods.py.j2")
        out = out_dir / f"__smt__{submethod.name}.py".lower()
        body = template.render(submethod=submethod)
        out.write_text(f"{_SUBMETHOD_HASH_PREFIX}{file_hash}\n{body}", encoding="utf-8")
        print(f"Generated: {out}")
        return out

    @classmethod
    def _submethod_hash(cls, smt_source: Path, hsi_path: Path) -> str:
        env = cls._jinja_env()
        template_src, _, _ = env.loader.get_source(env, "submethods.py.j2")
        h = hashlib.sha256()
        h.update(smt_source.read_bytes())
        h.update(b"\0")
        h.update(hsi_path.read_bytes())
        h.update(b"\0")
        h.update(template_src.encode("utf-8"))
        h.update(b"\0")
        h.update(_SUBMETHOD_CODEGEN_SALT)
        return h.hexdigest()

    @classmethod
    def _layout_hash(cls, layout: Path, res: Path) -> str:
        env = cls._jinja_env()
        template_src, _, _ = env.loader.get_source(env, "deck_layout.py.j2")
        h = hashlib.sha256()
        h.update(layout.read_bytes())
        h.update(b"\0")
        if res.exists():
            h.update(res.read_bytes())
        h.update(b"\0")
        h.update(template_src.encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def _read_hash(py_file: Path, prefix: str) -> str | None:
        if not py_file.exists():
            return None
        try:
            with py_file.open("r", encoding="utf-8") as f:
                first = f.readline().rstrip("\n")
        except OSError:
            return None
        if first.startswith(prefix):
            return first[len(prefix) :]
        return None

    @classmethod
    def _read_submethod_hash(cls, py_file: Path) -> str | None:
        return cls._read_hash(py_file, _SUBMETHOD_HASH_PREFIX)

    @classmethod
    def _parse_hxcfg_source(cls, source: Path) -> dict[str, Any]:
        """Parse an HxCfg file from its original location (converts in temp)."""
        return cls._parse_hxcfg_text(_read_ascii(source))

    @classmethod
    def _parse_hxcfg(cls, file: Path) -> dict[str, Any]:
        return cls._parse_hxcfg_text(file.read_text(encoding="utf-8", errors="replace"))

    @staticmethod
    def _parse_hxcfg_text(content: str) -> dict[str, Any]:
        content = re.sub(r"\\0x([A-Za-z0-9]{2})", _hex_replace, content)
        block_re = re.compile(r'DataDef,HxPars,3,(\w+),\n\[\n([\s\S]*?)"\)"\n\];')
        line_re = re.compile(r'"([()]?)(.*)",')

        out = "{\n"
        for b in block_re.finditer(content):
            out += f'"{b.group(1)}": {{\n'
            value_line = False
            for line in b.group(2).splitlines():
                if not line:
                    continue
                m = line_re.match(line)
                if not m:
                    continue
                if value_line:
                    out += f'"{m.group(2)}",\n'
                    value_line = False
                    continue
                if m.group(1) == "(":
                    out += f'"{m.group(2)}": {{\n'
                    continue
                if m.group(1) == ")":
                    out = out.rstrip(",\n")
                    out += "\n},\n"
                    continue
                out += f'"{m.group(2)}": '
                value_line = True
            out = out.rstrip(",\n")
            out += "},\n"
        out = out.rstrip(",\n") + "}"
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _build_comment_lookup(
        smt_def: dict[str, Any],
    ) -> dict[str, tuple[str, dict[str, str]]]:
        lookup: dict[str, tuple[str, dict[str, str]]] = {}
        submethods_block = smt_def.get("HxMetEd_Submethods", {}).get("-533725162", {})
        if not isinstance(submethods_block, dict):
            return lookup
        for entry in submethods_block.values():
            if not isinstance(entry, dict):
                continue
            name = entry.get("1-533725161", "")
            comment = entry.get("1-533725170", "")
            param_block = entry.get("-533725169", {}) or {}
            param_comments: dict[str, str] = {}
            if isinstance(param_block, dict):
                for pentry in param_block.values():
                    if isinstance(pentry, dict):
                        pname = pentry.get("1-533725168", "")
                        pcomment = pentry.get("1-533725167", "")
                        if pname:
                            param_comments[pname] = pcomment
            lookup[name] = (comment, param_comments)
        return lookup

    @staticmethod
    def _regenerate_init(out_dir: Path) -> None:
        lines: list[str] = []
        for py in sorted(out_dir.glob("*.py")):
            stem = py.stem
            if stem == "__init__":
                continue
            if stem == "__liquid_classes__":
                lines.append(f"from .{stem} import LiquidClasses")
            elif stem == "__labware__":
                lines.append(f"from . import {stem} as labware")
            elif stem.startswith("__layout__"):
                name = stem.removeprefix("__layout__")
                cls_name = name[:1].upper() + name[1:].lower()
                lines.append(f"from .{stem} import {cls_name}")
            elif stem.startswith("__smt__"):
                name = stem.removeprefix("__smt__")
                cls_name = name[:1].upper() + name[1:].lower()
                lines.append(f"from .{stem} import {cls_name}")
        (out_dir / "__init__.py").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
