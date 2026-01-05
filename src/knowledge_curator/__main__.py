"""Entry point for Knowledge Curator daemon.

Usage:
    python -m knowledge_curator [--config CONFIG_PATH]

Or via pixi:
    pixi run curator
    pixi run curator-dev  # With debug logging
"""

import argparse
import asyncio
from pathlib import Path

from knowledge_curator.daemon import run_daemon


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Knowledge Curator - LLM-powered curation daemon for UCKN"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Path to YAML configuration file",
    )
    args = parser.parse_args()

    asyncio.run(run_daemon(args.config))


if __name__ == "__main__":
    main()
