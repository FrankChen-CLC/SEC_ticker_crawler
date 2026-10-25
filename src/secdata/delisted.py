"""Build a historical ticker and delisting universe from public SEC data."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from . import config

COMMAND = "delisted"
HELP = "historical ticker and delisting universe (SEC + archived symbol lists)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("list", "status", "run", "check", "manifest"))
    parser.add_argument("--data-dir", type=Path,
                        help="working directory (default <out>/delisted_universe)")
    parser.add_argument("--from", dest="start_stage", metavar="STAGE")
    parser.add_argument("--only", metavar="STAGES", help="comma-separated stage IDs")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--stale", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-going", action="store_true")


def run(args: argparse.Namespace) -> int:
    root = (args.data_dir or config.out_dir() / "delisted_universe").resolve()
    if args.action == "run" and not args.dry_run:
        if config.offline():
            raise RuntimeError("delisted workflow cannot fetch data in offline mode")
        config.ua()
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SEC_DELISTED_ROOT"] = str(root)
    env["SEC_UA"] = env.get("SEC_EDGAR_UA", "")
    env["SEC_SLEEP"] = env.get("SEC_EDGAR_SLEEP", "0.15")
    script = Path(__file__).with_name("delisted_universe") / "scripts" / "workflow.py"
    cmd = [sys.executable, "-I", str(script), args.action]
    if args.action == "run":
        if args.start_stage:
            cmd.extend(("--from", args.start_stage))
        if args.only:
            cmd.extend(("--only", args.only))
        for flag in ("force", "stale", "dry_run", "keep_going"):
            if getattr(args, flag):
                cmd.append("--" + flag.replace("_", "-"))
    return subprocess.call(cmd, cwd=root, env=env)
