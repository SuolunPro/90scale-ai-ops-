"""Compatibility entry point for the former ops watchdog."""
from __future__ import annotations

import argparse
import pathlib
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ops import inspector


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/ops.json")
    parser.add_argument("--root", default=".")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--resolve-only", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    if args.apply:
        parser.error("请使用现有公开自检入口 public-refresh-audit.yml；本入口只读")
    return inspector.main(["--config", args.config, "--root", args.root,
                           "--out", "state/watchdog/latest.json",
                           "--only", "engine_runs,engine_fresh,site_data_sync,self_heal"])


if __name__ == "__main__":
    sys.exit(main())
