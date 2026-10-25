"""secdata command line.

Each pipeline module exposes three names: COMMAND (subcommand name), HELP
(one-liner) and add_arguments(parser) + run(args) -> int.

    export SEC_EDGAR_UA="Jane Doe jane@example.com"
    secdata insider --days-back 1 --out out
    secdata xbrl AAPL MSFT
"""
import argparse
import importlib
import sys

from . import __version__

MODULES = [
    "filings", "reports", "xbrl", "prexbrl", "holdings",
    "insider", "events", "proxy", "preipo", "enforcement", "delisted",
]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="secdata",
        description="Fetch U.S. stock data from SEC EDGAR.",
    )
    ap.add_argument("--version", action="version", version=f"secdata {__version__}")
    ap.add_argument("--ua", help="EDGAR User-Agent with contact info "
                                 "(overrides SEC_EDGAR_UA for this run)")
    ap.add_argument("--out", help="output directory (default $SEC_EDGAR_OUT or ./out)")
    ap.add_argument("--proxy", help="optional http proxy for SEC, e.g. http://host:port")
    ap.add_argument("--offline", action="store_true",
                    help="serve from cache only; never hit the network")
    sub = ap.add_subparsers(dest="command", metavar="<pipeline>")
    for name in MODULES:
        mod = importlib.import_module(f"secdata.{name}")
        sp = sub.add_parser(mod.COMMAND, help=mod.HELP)
        mod.add_arguments(sp)
        sp.set_defaults(_module=mod)
    return ap


def main(argv=None) -> int:
    import os
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "command", None):
        ap.print_help()
        return 2
    if getattr(args, "ua", None):
        os.environ["SEC_EDGAR_UA"] = args.ua
    if getattr(args, "out", None):
        os.environ["SEC_EDGAR_OUT"] = args.out
    if getattr(args, "proxy", None):
        os.environ["SEC_PROXY"] = args.proxy
    if getattr(args, "offline", False):
        os.environ["SEC_EDGAR_OFFLINE"] = "1"
    try:
        return args._module.run(args) or 0
    except RuntimeError as e:
        if "SEC_EDGAR_UA is not set" in str(e):
            print(f"error: {e}", file=sys.stderr)
            print('hint: secdata --ua "Your Name you@example.com" <pipeline> ... '
                  "or export SEC_EDGAR_UA", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
