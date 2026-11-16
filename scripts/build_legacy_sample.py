#!/usr/bin/env python3
"""CLI utility to extract an isolated subset of legacy database for testing."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from quant.migrate.legacy import build_sample

DEFAULT_20_TICKERS = [
    "360ONE.NS", "3MINDIA.NS", "AADHARHFC.NS", "AARTIIND.NS", "AAVAS.NS",
    "ABB.NS", "ABBOTINDIA.NS", "ABCAPITAL.NS", "ABDL.NS", "ABFRL.NS",
    "ABLBL.NS", "ABREL.NS", "ABSLAMC.NS", "ACC.NS", "ACE.NS",
    "ACMESOLAR.NS", "ACUTAAS.NS", "ADANIENSOL.NS", "ADANIENT.NS", "ADANIGREEN.NS",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract legacy database sample")
    parser.add_argument("--source", type=str, default="quant_engine.db", help="Path to source legacy database")
    parser.add_argument("--output", type=str, default="tests/fixtures/legacy_sample.db", help="Path to output sample database")
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated list of tickers")
    args = parser.parse_args()

    source = Path(args.source)
    output = Path(args.output)
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = DEFAULT_20_TICKERS

    res = build_sample(source=source, output=output, tickers=tickers)
    print(f"Sample built at {output}: {res.counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
