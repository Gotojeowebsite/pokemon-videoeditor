from __future__ import annotations

import argparse
from pathlib import Path

from src.config import load_config
from src.pipeline import run_batch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pokemon gameplay auto-editor with team overlays and RIP inserts."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root directory (defaults to current directory).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force processing even when duplicate confidence is high.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.root.resolve())
    run_batch(config=config, force=args.force)


if __name__ == "__main__":
    main()
