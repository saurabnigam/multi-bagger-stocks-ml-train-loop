from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from quant.errors import Blocked, Refused

_COMMAND_REGISTRY: dict[str, dict[str, tuple[Callable, str]]] = {}


def register(group: str, name: str, handler: Callable, help_text: str) -> None:
    """Register a command handler under a command group."""
    if group not in _COMMAND_REGISTRY:
        _COMMAND_REGISTRY[group] = {}
    _COMMAND_REGISTRY[group][name] = (handler, help_text)


def _init_default_commands():
    try:
        import quant.commands.universe
        import quant.commands.prices
        import quant.commands.data
        import quant.commands.factors
        import quant.commands.model
        import quant.commands.evaluate
    except ImportError:
        pass

    if "db" not in _COMMAND_REGISTRY:
        register("db", "init", lambda args: 0, "Initialize database schema")
        register("db", "verify", lambda args: 0, "Verify database ledger")
    if "data" not in _COMMAND_REGISTRY:
        register("data", "capture", lambda args: 0, "Capture vendor data")
    if "run" not in _COMMAND_REGISTRY:
        register("run", "monthly", lambda args: 0, "Execute monthly run pipeline")
    if "status" not in _COMMAND_REGISTRY:
        register("status", "show", lambda args: 0, "Show quant engine status")


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point with standardized return codes."""
    if argv is None:
        argv = sys.argv[1:]

    _init_default_commands()

    parser = argparse.ArgumentParser(prog="quant", description="V2 Quant Engine CLI")
    subparsers = parser.add_subparsers(dest="group", help="Command groups")

    # Top-level standalone commands e.g. status
    status_parser = subparsers.add_parser("status", help="Show quant engine status")
    status_parser.set_defaults(group="status", command="show")

    group_parsers = {}
    for grp, cmds in _COMMAND_REGISTRY.items():
        if grp == "status":
            continue
        grp_parser = subparsers.add_parser(grp, help=f"{grp} commands")
        group_parsers[grp] = grp_parser
        cmd_subparsers = grp_parser.add_subparsers(dest="command", help=f"{grp} subcommands")
        for cmd_name, (handler, h_text) in cmds.items():
            cmd_p = cmd_subparsers.add_parser(cmd_name, help=h_text)
            cmd_p.add_argument("--commit", action="store_true", help="Commit changes")
            cmd_p.add_argument("--as-of", type=str, help="Target as-of date (YYYY-MM-DD)")
            cmd_p.add_argument("--actor-kind", type=str, default="system", help="Actor kind (human|llm|system)")
            cmd_p.add_argument("--by", type=str, default="system:cli", help="Actor identifier")
            cmd_p.add_argument("--note", type=str, help="Rationale/note")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code

    if not args.group:
        parser.print_help()
        return 0

    group_dict = _COMMAND_REGISTRY.get(args.group, {})
    cmd_tuple = group_dict.get(getattr(args, "command", None))

    if not cmd_tuple:
        if args.group in group_parsers:
            group_parsers[args.group].print_help()
        else:
            parser.print_help()
        return 0

    handler, _ = cmd_tuple
    try:
        res = handler(args)
        if isinstance(res, int):
            return res
        return 0
    except Blocked as exc:
        sys.stderr.write(f"Blocked({exc.code}): {exc.detail}\n")
        return 2
    except Refused as exc:
        sys.stderr.write(f"Refused({exc.code}): {exc.detail}\n")
        return 3
    except SystemExit as exc:
        return exc.code
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
