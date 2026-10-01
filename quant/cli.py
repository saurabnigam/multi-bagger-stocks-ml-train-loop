from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from quant.errors import Blocked, Refused

_COMMAND_REGISTRY: dict[str, dict[str, tuple[Callable, str]]] = {}


def register(group: str, name: str, handler: Callable, help: str) -> None:
    """Register a command handler under a command group."""
    if group not in _COMMAND_REGISTRY:
        _COMMAND_REGISTRY[group] = {}
    _COMMAND_REGISTRY[group][name] = (handler, help)


def _init_default_commands():
    """Import every command module. A missing module is an error, never a silent no-op."""
    import importlib

    for mod in (
        "quant.commands.db",
        "quant.commands.status",
        "quant.commands.universe",
        "quant.commands.prices",
        "quant.commands.data",
        "quant.commands.factors",
        "quant.commands.model",
        "quant.commands.evaluate",
        "quant.commands.portfolio",
        "quant.commands.kb",
        "quant.commands.triage",
        "quant.commands.migrate",
        "quant.commands.run",
        "quant.commands.verify",
    ):
        importlib.import_module(mod)


_PARSER_CACHE: dict[tuple, tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]] = {}


def _build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    """Build the argparse tree once per registry shape; a parser holds no per-parse state.

    Rebuilding it costs about 30 ms per call (every command carries the full flag set), which a
    test suite that calls ``main`` hundreds of times pays back in seconds. The cache key is the
    command names only, so every flag and help text must be added unconditionally: nothing here
    may vary with the environment, the clock or the arguments of a call.
    """
    key = tuple((grp, tuple(cmds)) for grp, cmds in _COMMAND_REGISTRY.items())
    cached = _PARSER_CACHE.get(key)
    if cached is not None:
        return cached

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
            cmd_p.add_argument("target", nargs="?", default=None, help="Target ID (e.g. proposal_id, decision_id)")
            cmd_p.add_argument("--commit", action="store_true", help="Commit changes")
            cmd_p.add_argument("--as-of", type=str, help="Target as-of date (YYYY-MM-DD)")
            cmd_p.add_argument("--actor-kind", type=str, default="system", help="Actor kind (human|llm|system)")
            cmd_p.add_argument("--by", type=str, default="system:cli", help="Actor identifier")
            cmd_p.add_argument("--note", type=str, help="Rationale/note")
            cmd_p.add_argument("--db-path", "--db", dest="db_path", type=str, help="State database path")
            cmd_p.add_argument("--config", type=str, help="Config path")
            cmd_p.add_argument("--cohort-id", type=str, help="Cohort identifier")
            cmd_p.add_argument("--through", type=str, help="Through date (YYYY-MM-DD)")
            cmd_p.add_argument("--dry-run", action="store_true", help="Perform dry run without writing")
            cmd_p.add_argument("--legacy-db", type=str, help="Legacy database path")
            cmd_p.add_argument("--skip-capture", action="store_true", help="Skip data capture phase")
            cmd_p.add_argument("--stop-after", type=str, help="Stop pipeline after given phase")
            cmd_p.add_argument("--push", action="store_true", help="Push changes to remote repository")
            cmd_p.add_argument("--output", type=str, help="Output path (db rebuild)")
            cmd_p.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
            cmd_p.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
            cmd_p.add_argument("--phase", type=str, help="Phase selector")
            cmd_p.add_argument("--track", type=str, help="Track (live|backfill|legacy|counterfactual)")
            cmd_p.add_argument("--isin", type=str, help="Security ISIN")
            cmd_p.add_argument("--symbol", type=str, help="Security NSE symbol")
            cmd_p.add_argument("--ex-date", dest="ex_date", type=str, help="Corporate action ex-date (YYYY-MM-DD)")
            cmd_p.add_argument("--kind", type=str, help="Corporate action / capture kind")
            cmd_p.add_argument("--factor", type=str, help="Corporate action value-transfer adjustment factor")
            cmd_p.add_argument("--ratio", type=str, help="Corporate action split/bonus ratio (new per old)")
            cmd_p.add_argument("--evidence", type=str, help="Evidence citation for a governance decision")
            cmd_p.add_argument("--title", type=str, help="Decision title override")
            cmd_p.add_argument("--force", action="store_true", help="Override an implausible-factor sanity check")
            cmd_p.add_argument("--all", action="store_true", help="Include resolved/inactive rows in a listing")
            cmd_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
            cmd_p.add_argument("--decision-id", dest="decision_id", type=str, help="Governing decision ID")
            cmd_p.add_argument("--event-id", dest="event_id", type=str, help="Data quality / corporate action event ID")
            cmd_p.add_argument("--months", type=int, help="Number of months to verify")
            cmd_p.add_argument("--verify", action="store_true", help="Perform verification (e.g. prices manifest --verify)")
            # Evidence triage (docs/operations/triage.md)
            cmd_p.add_argument("--since", type=str, help="Widen the triage anomaly window back to this date (YYYY-MM-DD)")
            cmd_p.add_argument("--proposal-id", dest="proposal_id", type=str, help="Triage proposal ID (PT-...)")
            cmd_p.add_argument("--confirm", type=str, help="First 8 hex of the proposal payload sha256 (data actions-resolve)")
            cmd_p.add_argument("--file", type=str, help="Document body (triage attach) or proposal JSON (triage submit)")
            cmd_p.add_argument("--url", type=str, help="Link to a document (triage attach); never fetched")
            cmd_p.add_argument("--published-at", dest="published_at", type=str,
                               help="When the source published the document: YYYY-MM-DD or a timestamp with timezone")
            cmd_p.add_argument("--excerpt", type=str, help="Excerpt of the document, at most 50 words")
            cmd_p.add_argument("--source-class", dest="source_class", type=str,
                               help="exchange|regulator|company|news|other (triage attach)")
            cmd_p.add_argument("--relevance", type=str, help="entity|sector|context (triage attach)")
            cmd_p.add_argument("--field", action="append", help="Typed field KEY=VALUE, repeatable (triage attach)")
            cmd_p.add_argument("--reason", type=str, help="Reason code, lower-case letters, digits and underscores (triage reject)")

    _PARSER_CACHE[key] = (parser, group_parsers)
    return parser, group_parsers


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point with standardized return codes."""
    if argv is None:
        argv = sys.argv[1:]

    _init_default_commands()
    parser, group_parsers = _build_parser()

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
