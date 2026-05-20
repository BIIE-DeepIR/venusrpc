"""venusrpc command-line entry point."""

from __future__ import annotations

import argparse
import shutil
import sys
from importlib import metadata
from pathlib import Path


def _resource_root() -> Path:
    return Path(__file__).resolve().parent


def _scaffold_root() -> Path:
    return _resource_root() / "scaffold"


def _cmd_version(_: argparse.Namespace) -> int:
    try:
        version = metadata.version("venusrpc")
    except metadata.PackageNotFoundError:
        from venusrpc import __version__ as version
    print(f"venusrpc {version}")
    return 0


def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "TOML parser missing. Use Python >=3.11 or `pip install tomli`."
            ) from exc
    with path.open("rb") as f:
        return tomllib.load(f)


def _resolve_config(explicit: str | None) -> tuple[dict, Path]:
    """Return (config_dict, base_dir). Raises if no config found."""
    if explicit:
        cfg_path = Path(explicit).resolve()
        data = _load_toml(cfg_path)
        # Allow either flat top-level keys OR `[tool.venusrpc]` nesting in any file.
        section = data.get("tool", {}).get("venusrpc", data)
        return section, cfg_path.parent

    cwd = Path.cwd()
    for fname in ("venusrpc.toml", "pyproject.toml"):
        p = cwd / fname
        if not p.exists():
            continue
        data = _load_toml(p)
        if fname == "pyproject.toml":
            section = data.get("tool", {}).get("venusrpc")
            if section is None:
                continue
        else:
            section = data.get("tool", {}).get("venusrpc", data)
        return section, p.parent

    raise RuntimeError(
        "No venusrpc config found. Add [tool.venusrpc] to pyproject.toml, "
        "or pass --config <file>."
    )


def _cmd_generate(args: argparse.Namespace) -> int:
    from venusrpc.codegen import Codegen

    try:
        cfg, base = _resolve_config(args.config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_dir = cfg.get("output_dir")
    output_path = (base / output_dir).resolve() if output_dir else None
    force = bool(args.force)

    layouts = cfg.get("layouts", [])
    if isinstance(layouts, str):
        layouts = [layouts]
    for lay in layouts:
        Codegen.read_layout(base / lay, output_dir=output_path, force=force)

    if cfg.get("submethods_dir") or cfg.get("submethods", True):
        smt_dir = cfg.get("submethods_dir")
        smt_path = (base / smt_dir).resolve() if smt_dir else None
        Codegen.read_submethods(directory=smt_path, output_dir=output_path, force=force)

    lc = cfg.get("liquid_classes")
    if isinstance(lc, dict) and lc.get("enabled", True):
        kwargs = {k: v for k, v in lc.items() if k != "enabled"}
        Codegen.read_liquid_classes(output_dir=output_path, **kwargs)

    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg, base = _resolve_config(args.config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    from venusrpc.codegen import Codegen

    output_dir = cfg.get("output_dir")
    output_path = (base / output_dir).resolve() if output_dir else None

    layouts = cfg.get("layouts", [])
    if isinstance(layouts, str):
        layouts = [layouts]
    layout_paths = [base / lay for lay in layouts]
    smt_dir_cfg = cfg.get("submethods_dir")
    smt_path = (base / smt_dir_cfg).resolve() if smt_dir_cfg else None

    stale = Codegen.verify(
        layouts=layout_paths,
        submethods_dir=smt_path,
        output_dir=output_path,
    )
    if not stale:
        print("venusrpc: generated wrappers are up-to-date.")
        return 0

    msg = "Stale or missing generated wrappers (run `venusrpc generate`):"
    print(msg, file=sys.stderr)
    for src, reason in stale:
        print(f"  [{reason}] {src}", file=sys.stderr)
    if args.warn:
        return 0
    return 2


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    src = _scaffold_root()
    if not src.exists():
        print(f"scaffold directory missing: {src}", file=sys.stderr)
        return 1
    for item in src.iterdir():
        dest = target / item.name
        if dest.exists() and not args.force:
            print(f"skip (exists): {dest}")
            continue
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=args.force)
        else:
            shutil.copy2(item, dest)
        print(f"wrote {dest}")
    return 0


def _cmd_simulation(args: argparse.Namespace) -> int:
    from venusrpc.simulation import (
        DEFAULT_CONFIG_DIR,
        get_simulation,
        set_simulation,
    )

    cfg_dir = Path(args.config_dir) if args.config_dir else None
    try:
        if args.action == "status":
            states = get_simulation(cfg_dir)
        else:
            states = set_simulation(args.action == "on", cfg_dir)
    except PermissionError as exc:
        print(f"permission denied (need admin?): {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for s in states:
        if s.enabled is None:
            print(f"{s.label} = <missing> ({s.path})")
        else:
            print(f"{s.label} = {'ON' if s.enabled else 'OFF'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="venusrpc")
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="show installed version")
    p_version.set_defaults(func=_cmd_version)

    p_init = sub.add_parser("init", help="scaffold a new venusrpc project")
    p_init.add_argument("directory", nargs="?", default=".", help="target directory")
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.set_defaults(func=_cmd_init)

    p_gen = sub.add_parser(
        "generate",
        help="regenerate Python wrappers from venusrpc config (pyproject.toml or venusrpc.toml)",
    )
    p_gen.add_argument(
        "--config",
        default=None,
        help="explicit config file path (default: auto-detect venusrpc.toml or pyproject.toml in cwd)",
    )
    p_gen.add_argument(
        "--force",
        action="store_true",
        help="regenerate every wrapper even if source hash matches (bypasses skip-by-hash)",
    )
    p_gen.set_defaults(func=_cmd_generate)

    p_check = sub.add_parser(
        "check",
        help="verify generated wrappers match their source hashes (CI guard)",
    )
    p_check.add_argument(
        "--config",
        default=None,
        help="explicit config file path (default: auto-detect)",
    )
    p_check.add_argument(
        "--warn",
        action="store_true",
        help="print stale entries but exit 0 instead of 2",
    )
    p_check.set_defaults(func=_cmd_check)

    p_sim = sub.add_parser(
        "simulation",
        help="read or set Hamilton Venus simulation flags (needs admin to write)",
    )
    p_sim.add_argument("action", choices=("status", "on", "off"))
    p_sim.add_argument(
        "--config-dir",
        default=None,
        help=r"Venus config directory (default: C:\Program Files (x86)\HAMILTON\Config)",
    )
    p_sim.set_defaults(func=_cmd_simulation)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
