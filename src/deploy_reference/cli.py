"""deploy-reference command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deploy-reference", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("e2e", help="build, deploy and test on a local kind cluster, then delete it")
    e.add_argument("--keep", action="store_true", help="keep the cluster afterwards")
    e.add_argument("--reuse", action="store_true", help="use an existing cluster (never deleted)")
    e.add_argument("--skip-build", action="store_true", help="reuse the local image")
    e.add_argument("--load-seconds", type=int, default=120)
    e.add_argument("--out", default="reports")
    args = p.parse_args(argv)
    if args.cmd == "e2e":
        from deploy_reference.e2e import Runner

        return Runner(args.keep, args.skip_build, args.load_seconds, args.reuse).go(Path(args.out))
    return 2


if __name__ == "__main__":
    sys.exit(main())
